"""
发票版面叠印检测与主层选择

用于识别 PDF 同页多套版式（A/B 叠印），选择主层边界，并在明细解析时清理噪声行/词。
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import invoice_const

from .regex_utils import (
    DATE_REGEX_LOOSE,
    PROJECT_NAME_REGEX,
    has_project_name_at_start,
    iter_valid_project_name_matches,
)
from .text_utils import clean_garbled_chars

logger = logging.getLogger(__name__)

ANCHOR_Y_GAP = 60.0
# 两个表头 Y 间距超过此值且中间有「第2页」等标记时，视为多页模版而非同页叠印
MULTI_PAGE_TEMPLATE_GAP = 200.0
HEADER_DATE_SEARCH_ABOVE = 250.0
NEXT_HEADER_MARGIN = 5.0
OVERLAY_Y_OFFSET_TOLERANCE = 18.0
CONTINUATION_LINE_GAP = 9.0
CONTINUATION_Y_TOLERANCE = 6.0
STACKED_X_TOLERANCE = 25.0

BUYER_SELLER_ROW_KEYWORDS = [
    '名称：', '名称:', '统一社会信用代码', '纳税人识别号', '购买方', '销售方',
]
BUYER_SELLER_FRAGMENT_KEYWORDS = ['购', '销', '买', '售', '信', '息']
WORD_NOISE_KEYWORDS = [
    '名称：', '名称:', '统一社会信用代码', '纳税人识别号', '购买方', '销售方',
    '购', '销', '买', '售',
]
TAX_ID_WORD_PATTERN = re.compile(r'^[0-9A-Z]{15,20}$')
AMOUNT_LIKE_PATTERN = re.compile(r'^\d+\.?\d*$|^\d+%$')
ITEM_NAME_START_PATTERN = re.compile(r'^\*[^*]+\*')
NUMERIC_COLUMN_PATTERN = re.compile(r'^-?[\d.]+%?$')
COMPANY_NAME_PATTERN = re.compile(r'(公司|有限公司|服务中心|餐饮|烟草公司)')
INVOICE_HEADER_WORDS = [
    '电子发票', '增值税专用发票', '普通发票', '发票号码', '开票日期',
]
ALWAYS_DROP_EXACT = frozenset(BUYER_SELLER_FRAGMENT_KEYWORDS)


@dataclass
class LayoutBounds:
    """主层分区边界（用于覆盖默认可关键词边界）"""
    has_overlay: bool
    primary_header_y: float
    header_last_y: float
    table_first_y: float
    duplicate_header_ys: List[float] = field(default_factory=list)
    overlay_layer_start_y: Optional[float] = None
    overlay_y_offset: Optional[float] = None
    score: float = 0.0


def _row_text(line_words: List[dict]) -> str:
    return ' '.join(w['text'] for w in sorted(line_words, key=lambda w: w['x0']))


def _row_text_no_space(line_words: List[dict]) -> str:
    return _row_text(line_words).replace(' ', '')


def count_table_header_keywords(line_words: List[dict]) -> int:
    text = _row_text_no_space(line_words)
    return sum(1 for kw in invoice_const.TABLE_KEYWORDS if kw in text)


def is_table_header_row(line_words: List[dict]) -> bool:
    return count_table_header_keywords(line_words) >= invoice_const.MIN_HEADER_KEYWORDS


def find_table_header_rows(lines_dict: dict) -> List[float]:
    headers = []
    for y, line_words in lines_dict.items():
        if is_table_header_row(line_words):
            headers.append(y)
    return sorted(headers)


def find_header_last_y_before(lines_dict: dict, before_y: float) -> Optional[float]:
    """
    在表头行之上（top 更小）找开票日期/日期值，取最靠近表头的行（top 最大且 < before_y）。
    """
    candidates = []
    for y, line_words in lines_dict.items():
        if y >= before_y or (before_y - y) > HEADER_DATE_SEARCH_ABOVE:
            continue
        text = _row_text(line_words)
        if '开票日期' in text or DATE_REGEX_LOOSE.search(text):
            candidates.append(y)
    return max(candidates) if candidates else None


def _next_header_y(header_y: float, all_headers: List[float]) -> Optional[float]:
    later = [y for y in all_headers if y > header_y + NEXT_HEADER_MARGIN]
    return min(later) if later else None


def count_item_rows_between(lines_dict: dict, y_start: float, y_end: Optional[float]) -> int:
    count = 0
    for y, line_words in lines_dict.items():
        if y <= y_start + NEXT_HEADER_MARGIN:
            continue
        if y_end is not None and y >= y_end - NEXT_HEADER_MARGIN:
            continue
        text = _row_text(line_words)
        if PROJECT_NAME_REGEX.search(text):
            count += 1
            continue
        if any(kw in text for kw in invoice_const.SUMMARY_ROW_KEYWORDS):
            continue
        if re.search(r'\d+\.\d{2}', text) and re.search(r'[\u4e00-\u9fa5]', text):
            count += 1
    return count


def has_buyer_block_before(lines_dict: dict, before_y: float) -> bool:
    for y, line_words in lines_dict.items():
        if y >= before_y:
            continue
        text = _row_text(line_words)
        if '名称' in text and ('购' in text or '买' in text or '公司' in text):
            return True
        if '统一社会信用代码' in text or '纳税人识别号' in text:
            return True
    return False


def _has_multi_page_template_marker(lines_dict: dict, y_low: float, y_high: float) -> bool:
    """两个表头之间或第二个表头附近是否出现多页标记（如「共2页 第2页」）"""
    for y, line_words in lines_dict.items():
        if y <= y_low or y > y_high + 60.0:
            continue
        text = _row_text(line_words).replace(' ', '')
        if re.search(r'共\d+页', text) or re.search(r'第[2-9]\d*页', text):
            return True
    return False


def _page_numbers_near_header(
    lines_dict: dict, header_y: float, radius: float = 120.0,
) -> Set[int]:
    """表头附近行中的「第N页」页码集合"""
    pages: Set[int] = set()
    for y, line_words in lines_dict.items():
        if abs(y - header_y) > radius:
            continue
        text = _row_text(line_words).replace(' ', '')
        for match in re.finditer(r'第(\d+)页', text):
            pages.add(int(match.group(1)))
    return pages


def _is_sequential_multi_page_template(
    lines_dict: dict, first_header_y: float, second_header_y: float,
) -> bool:
    """
    大间距双表头是否为「第1页 + 第2页」顺序多页模版（非叠印）。

    若两个表头附近的分页标记相同（如均为「第2页」），则为同页叠印副本，
    不应按多页模版排除叠印检测。
    """
    pages_first = _page_numbers_near_header(lines_dict, first_header_y)
    pages_second = _page_numbers_near_header(lines_dict, second_header_y)
    if not pages_first and not pages_second:
        return False
    if pages_first and pages_second:
        if pages_first & pages_second:
            return False
        if min(pages_second) > max(pages_first):
            return True
        return False
    if pages_first and not pages_second:
        return max(pages_first) <= 1
    if pages_second and not pages_first:
        return min(pages_second) >= 2
    return False


def detect_overlay(lines_dict: dict) -> bool:
    """检测是否存在多套版式（叠印）"""
    if not lines_dict:
        return False

    headers = find_table_header_rows(lines_dict)
    if len(headers) >= 2 and (headers[-1] - headers[0]) >= ANCHOR_Y_GAP:
        gap = headers[-1] - headers[0]
        if gap >= MULTI_PAGE_TEMPLATE_GAP and _has_multi_page_template_marker(
            lines_dict, headers[0], headers[-1],
        ):
            if _is_sequential_multi_page_template(lines_dict, headers[0], headers[-1]):
                logger.debug(
                    '检测到多页模版（表头间距=%.2f，顺序分页标记），不按叠印处理',
                    gap,
                )
                return False
            logger.debug(
                '大间距双表头但分页标记相同（表头间距=%.2f），仍按叠印处理',
                gap,
            )
        return True

    global_header_last = None
    global_table_first = None
    for y, line_words in lines_dict.items():
        text = _row_text(line_words)
        if '开票日期' in text or DATE_REGEX_LOOSE.search(text):
            if global_header_last is None or y > global_header_last:
                global_header_last = y
        if is_table_header_row(line_words):
            if global_table_first is None or y < global_table_first:
                global_table_first = y

    if global_header_last is not None and global_table_first is not None:
        if global_header_last > global_table_first:
            return True

    return False


def _layout_candidate(
    lines_dict: dict,
    header_y: float,
    headers: List[float],
    has_overlay: bool,
) -> LayoutBounds:
    header_last = find_header_last_y_before(lines_dict, header_y)
    layout_valid = header_last is not None and header_y > header_last
    next_y = _next_header_y(header_y, headers)
    item_count = count_item_rows_between(lines_dict, header_y, next_y)
    score = (1000.0 if layout_valid else 0.0) + item_count * 10.0
    if has_buyer_block_before(lines_dict, header_y):
        score += 100.0
    if header_y < 0:
        score += 50.0
    # 叠印时主层表头通常更靠上（Y 更小）；B 套重复表头在下方（Y 更大）
    if has_overlay and len(headers) >= 2:
        if header_y == min(headers):
            score += 500.0
        if header_y == max(headers):
            score -= 400.0

    duplicate_ys = [y for y in headers if y > header_y + NEXT_HEADER_MARGIN]
    overlay_start = min(duplicate_ys) + 9.0 if has_overlay and duplicate_ys else None
    overlay_offset = (duplicate_ys[0] - header_y) if has_overlay and duplicate_ys else None

    return LayoutBounds(
        has_overlay=has_overlay,
        primary_header_y=header_y,
        header_last_y=header_last if header_last is not None else header_y - 1,
        table_first_y=header_y,
        duplicate_header_ys=duplicate_ys,
        overlay_layer_start_y=overlay_start,
        overlay_y_offset=overlay_offset,
        score=score,
    )


def compute_layout_bounds(lines_dict: dict) -> Optional[LayoutBounds]:
    """
    为当前页选择主层表头，并计算分区用的 header_last_y / table_first_y。
    """
    headers = find_table_header_rows(lines_dict)
    if not headers:
        return None

    has_overlay = detect_overlay(lines_dict)
    best: Optional[LayoutBounds] = None

    for header_y in headers:
        candidate = _layout_candidate(lines_dict, header_y, headers, has_overlay)
        if best is None or candidate.score > best.score:
            best = candidate

    if best:
        logger.debug(
            '版面主层: has_overlay=%s, primary_header_y=%.2f, header_last_y=%.2f, '
            'table_first_y=%.2f, score=%.1f, duplicate_headers=%s',
            best.has_overlay, best.primary_header_y, best.header_last_y,
            best.table_first_y, best.score,
            [f'{y:.2f}' for y in best.duplicate_header_ys if abs(y - best.primary_header_y) > NEXT_HEADER_MARGIN],
        )
    return best


def filter_duplicate_header_rows(lines_dict: dict, layout: LayoutBounds) -> dict:
    """移除非主层的重复表头行（仅移除主层表头下方的 B 套表头，不删主层）"""
    if not layout.has_overlay:
        return lines_dict

    headers = find_table_header_rows(lines_dict)
    remove_ys = {y for y in headers if y > layout.primary_header_y + NEXT_HEADER_MARGIN}
    if not remove_ys:
        return lines_dict

    filtered = {y: words for y, words in lines_dict.items() if y not in remove_ys}
    logger.debug('叠印清理: 移除重复表头行 Y=%s', [f'{y:.2f}' for y in sorted(remove_ys)])
    return filtered


def apply_layout_preprocessing(lines_dict: dict) -> Tuple[dict, Optional[LayoutBounds]]:
    """
    检测叠印、选择主层、移除重复表头行。

    Returns:
        (processed_lines_dict, layout_bounds or None)
    """
    layout = compute_layout_bounds(lines_dict)
    if layout is None:
        return lines_dict, None

    overlay_detected = layout.has_overlay
    processed = filter_duplicate_header_rows(lines_dict, layout)

    # 主层边界合法时始终使用（不仅依赖过滤后是否仍能检出叠印）
    if layout.table_first_y > layout.header_last_y:
        layout.has_overlay = overlay_detected
        return processed, layout

    logger.warning(
        '主层边界不合法，跳过叠印边界: header_last_y=%.2f, table_first_y=%.2f',
        layout.header_last_y, layout.table_first_y,
    )
    return processed, None


def is_summary_row_text(row_text: str) -> bool:
    text = row_text.replace(' ', '')
    return any(kw in text for kw in invoice_const.SUMMARY_ROW_KEYWORDS)


def is_invoice_header_noise_row(line_words: List[dict]) -> bool:
    """叠印层发票头碎片（开票日期、页码等），无商品结构"""
    row_text = _row_text(line_words)
    text = row_text.replace(' ', '')
    if PROJECT_NAME_REGEX.search(row_text):
        return False
    if '开票日期' in text or DATE_REGEX_LOOSE.search(row_text):
        if not re.search(r'\d+\.\d{2}', text):
            return True
    if '发票号码' in text and not PROJECT_NAME_REGEX.search(row_text):
        return True
    if any(kw in text for kw in INVOICE_HEADER_WORDS):
        if not PROJECT_NAME_REGEX.search(row_text) and not re.search(r'\d+\.\d{2}', text):
            return True
    if '共' in text and '页' in text and '第' in text:
        return True
    return False


def count_item_name_starts(line_words: List[dict]) -> int:
    count = 0
    for w in sorted(line_words, key=lambda x: x['x0']):
        t = clean_garbled_chars(w.get('text', '')).strip()
        if ITEM_NAME_START_PATTERN.search(t):
            count += 1
    return count


def count_products_in_row(line_words: List[dict]) -> int:
    """一行内 *类别*商品名 模式出现次数（排除规格中的 mm*45米 等误匹配）"""
    return len(list(iter_valid_project_name_matches(_row_text(line_words))))


def is_multi_product_merged_row(line_words: List[dict]) -> bool:
    return count_products_in_row(line_words) >= 2


def strip_orphan_prefix_words(line_words: List[dict]) -> List[dict]:
    """
    去掉首个 *类别*商品名 左侧的孤儿续行片段（如「楼）」「钗烤烟)」），
    避免与下一商品名拼在同一明细里。
    """
    _, stripped = extract_orphan_prefix_from_row(line_words)
    return stripped


def extract_orphan_prefix_from_row(line_words: List[dict]) -> Tuple[str, List[dict]]:
    """
    剥离首个 *类别* 商品名左侧的续行碎片。

    Returns:
        (prefix_text, remaining_words)
    """
    if not line_words:
        return '', line_words
    sorted_words = sorted(line_words, key=lambda w: w['x0'])
    first_idx = None
    for i, w in enumerate(sorted_words):
        t = clean_garbled_chars(w.get('text', '')).strip()
        if has_project_name_at_start(t) or ITEM_NAME_START_PATTERN.search(t):
            first_idx = i
            break
    if first_idx is None or first_idx == 0:
        return '', line_words
    prefix_text = _row_text(sorted_words[:first_idx]).replace(' ', '').strip()
    if PROJECT_NAME_REGEX.search(prefix_text):
        return '', line_words
    return prefix_text, sorted_words[first_idx:]


def is_orphan_suffix_fragment_text(text: str) -> bool:
    """文本是否像上一商品名的折行 suffix（非完整 *类别* 首行）"""
    compact = (text or '').replace(' ', '').strip()
    if not compact or len(compact) > 20:
        return False
    if is_buyer_seller_fragment_text(compact):
        return False
    if DATE_REGEX_LOOSE.search(compact):
        return False
    if has_project_name_at_start(compact):
        return False
    if re.search(r'\d+\.\d', compact) and not re.search(r'[）)]$', compact):
        return False
    # 规格尾行如「套装含钴6.」：含中文+数字+句点，整段拼接而非 orphan suffix
    if re.search(r'[\u4e00-\u9fa5].*\d+\.?$', compact) and len(compact) > 4:
        return False
    if re.search(r'[）)]', compact):
        return True
    return len(compact) <= 8 and bool(re.search(r'[\u4e00-\u9fa5]', compact))


def _name_needs_continuation(name: str) -> bool:
    if not name or not name.strip():
        return False
    n = name.strip().replace(' ', '')
    if n.count('（') > n.count('）'):
        return True
    if n.count('(') > n.count(')'):
        return True
    if re.search(r'[（(][^）)]*$', n):
        return True
    return False


def merge_item_name_suffix(parent_name: str, suffix: str) -> str:
    """
    将折行 suffix 拼接到 parent 名称，处理 PDF 折行重复字符。

    例: 「长白山（77」+「7）」→「长白山（77）」（非「777）」）
    """
    parent_c = (parent_name or '').replace(' ', '')
    suffix_c = (suffix or '').replace(' ', '')
    if not suffix_c:
        return parent_c

    for close in ('）', ')'):
        if suffix_c.endswith(close):
            body = suffix_c[:-1]
            if len(body) == 1 and parent_c.endswith(body):
                return parent_c + close
            break

    return parent_c + suffix_c


def suffix_completes_parent_name(parent_name: str, suffix: str) -> bool:
    """
    续行 suffix 是否与 parent 括号内文字匹配并能闭合名称。

    叠印 PDF 中 orphan 常来自相邻商品（如 钗烤烟)、新版)），须按括号类型区分：
    - 全角「（…」且括号内已完整（望岳、颜悦）→ 只接受「）」
    - 半角「(…」折行 → 接受 mg)、精品) 等带正文的 suffix
    - 数字型号折行 → 接受 7） 闭合 77
    """
    parent_c = (parent_name or '').replace(' ', '')
    suffix_c = (suffix or '').replace(' ', '')
    if not parent_c or not suffix_c:
        return False
    if not _name_needs_continuation(parent_c):
        return False

    merged = merge_item_name_suffix(parent_c, suffix_c)
    if _name_needs_continuation(merged):
        return False

    open_match = re.search(r'([（(])([^（(）)]*)$', parent_c)
    if not open_match:
        return len(suffix_c) <= 15

    open_ch, prefix = open_match.group(1), open_match.group(2)
    body = re.sub(r'[）)]$', '', suffix_c)
    close_ch = '）' if open_ch == '（' else ')'

    if suffix_c == close_ch or not body:
        if suffix_c != close_ch:
            return False
        if open_ch == '（':
            return len(prefix) >= 2 and bool(re.search(r'[\u4e00-\u9fa5]', prefix))
        return len(prefix) >= 1

    if open_ch == '（':
        if not suffix_c.endswith('）'):
            return False
        if len(body) == 1 and body.isdigit() and prefix.endswith(body):
            return True
        if len(body) == 1 and re.match(r'[\u4e00-\u9fa5]', body):
            return True
        if body[0] in prefix or prefix[-1] == body[0]:
            return True
        prefix_is_complete = (
            len(prefix) >= 2
            and re.match(r'^[\u4e00-\u9fa5]+$', prefix)
        )
        foreign_markers = ('新版', '软混', '钗烤', '冰爵', '满堂')
        if prefix_is_complete and any(marker in body for marker in foreign_markers):
            return False
        return len(body) <= 8 and bool(re.search(r'[\u4e00-\u9fa5A-Za-z0-9]', body))

    if open_ch == '(':
        if not suffix_c.endswith(')'):
            return False
        if re.match(r'^[A-Za-z0-9]+$', body):
            return len(body) <= 6
        if re.search(r'[\u4e00-\u9fa5]', body):
            return len(body) <= 10
        return len(body) <= 8

    return suffix_c.endswith(')')


def close_item_name_if_paren_only(item_name: str) -> str:
    """括号内文字已完整、仅缺闭括号时自动补全"""
    if not item_name:
        return item_name
    compact = item_name.replace(' ', '')
    if not _name_needs_continuation(compact):
        return compact

    open_match = re.search(r'（([^（）]*)$', compact)
    if open_match:
        prefix = open_match.group(1)
        if prefix and re.match(r'^[\d.]+$', prefix):
            trial = compact + '）'
            if not _name_needs_continuation(trial):
                return trial
        if len(prefix) >= 2 and re.search(r'[\u4e00-\u9fa5]', prefix):
            trial = compact + '）'
            if not _name_needs_continuation(trial):
                return trial

    return compact


def peel_trailing_suffix_after_open_paren(text: str) -> str:
    """
    从「*类别*商品名（…」词末尾剥离叠入的续行 suffix。

    如「*烟草制品*黄金叶（金云龙）」→「云龙）」（「金」属本行商品，「云龙）」属上一行）。
    """
    compact = (text or '').replace(' ', '')
    match = re.search(
        r'(\*[^*]+\*[^*（(]*[（(][^）)]*?)([\u4e00-\u9fa5]{1,8}[）)])$',
        compact,
    )
    if not match:
        return ''
    suffix = match.group(2)
    if is_orphan_suffix_fragment_text(suffix):
        return suffix
    return ''


def split_mixed_row_orphan_suffix(
    item_name: str,
    peel_for_previous: bool = False,
) -> Tuple[str, str]:
    """
    从混排行商品名字符串中剥离属于上一行的续行 suffix。

    典型：Y=375 行「*烟草制品*黄金叶（金 云龙）」中，
    「云龙）」属于上一行 Y=366「云烟（细支」，黄金叶自身续行在 Y=384「满堂）」。
    """
    if not peel_for_previous or not item_name:
        return item_name, ''
    compact = item_name.replace(' ', '')
    match = PROJECT_NAME_REGEX.search(compact)
    if not match:
        return item_name, ''
    rest = compact[match.end():]
    if rest:
        suffix_match = re.match(r'^([\u4e00-\u9fa5][^*]{0,14}[）)])', rest)
        if suffix_match:
            suffix = suffix_match.group(1)
            if is_orphan_suffix_fragment_text(suffix):
                return compact[:match.end()], suffix

    embedded = peel_trailing_suffix_after_open_paren(compact)
    if embedded:
        base = compact[: -len(embedded)]
        if PROJECT_NAME_REGEX.search(base) and _name_needs_continuation(base):
            return base, embedded

    return item_name, ''


def peel_orphan_suffix_words_from_mixed_row(
    line_words: List[dict],
    col_x_ranges: Optional[dict],
    peel_for_previous: bool = False,
) -> Tuple[str, List[dict]]:
    """
    从混排行词列表中剥离 *类别* 首词与单位列之间的续行词（词级）。
    """
    if not peel_for_previous or not line_words:
        return '', line_words
    sorted_words = sorted(line_words, key=lambda w: w['x0'])
    unit_x0 = col_x_ranges.get('单位', (9999.0, 9999.0))[0] if col_x_ranges else 9999.0

    first_product_idx = None
    for i, w in enumerate(sorted_words):
        t = clean_garbled_chars(w.get('text', '')).strip()
        if has_project_name_at_start(t) or ITEM_NAME_START_PATTERN.search(t):
            first_product_idx = i
            break
    if first_product_idx is None:
        return '', line_words

    orphan_parts: List[str] = []
    keep_words = list(sorted_words[: first_product_idx + 1])
    for w in sorted_words[first_product_idx + 1:]:
        if w['x0'] >= unit_x0 - 5:
            keep_words.append(w)
            continue
        t = clean_garbled_chars(w.get('text', '')).strip().replace(' ', '')
        if not t:
            continue
        if has_project_name_at_start(t) or ITEM_NAME_START_PATTERN.search(t):
            keep_words.append(w)
            continue
        if is_orphan_suffix_fragment_text(t):
            orphan_parts.append(t)
        else:
            keep_words.append(w)
    return ''.join(orphan_parts), keep_words


def extract_orphan_suffix_for_previous_row(
    line_words: List[dict],
    col_x_ranges: Optional[dict],
    peel_for_previous: bool = False,
) -> Tuple[str, List[dict]]:
    """
    从混排行提取属于上一商品的续行 suffix（即使本行商品会被叠印逻辑跳过）。

    优先：左侧 prefix → 词级 inline → 商品名单词内末尾 suffix。
    """
    if not peel_for_previous or not line_words:
        return '', line_words

    prefix, words = extract_orphan_prefix_from_row(line_words)
    if prefix and is_orphan_suffix_fragment_text(prefix):
        return prefix, words

    inline, words = peel_orphan_suffix_words_from_mixed_row(
        words, col_x_ranges, peel_for_previous=True,
    )
    if inline:
        return inline, words

    unit_x0 = col_x_ranges.get('单位', (9999.0, 9999.0))[0] if col_x_ranges else 9999.0
    for w in sorted(words, key=lambda x: x['x0']):
        if w['x0'] >= unit_x0 - 5:
            break
        text = clean_garbled_chars(w.get('text', '')).strip()
        if not text or not PROJECT_NAME_REGEX.search(text):
            continue
        _, suffix = split_mixed_row_orphan_suffix(text, peel_for_previous=True)
        if suffix:
            return suffix, words

    return '', line_words


def is_buyer_seller_fragment_text(text: str) -> bool:
    """购销方/表头折行噪声（方方、购销等），不是商品名续行"""
    compact = text.replace(' ', '')
    if not compact:
        return False
    if re.fullmatch(r'[信息购销买卖方]+', compact):
        return True
    if compact in ALWAYS_DROP_EXACT:
        return True
    if len(compact) <= 4 and all(c in '购销买卖方信息' for c in compact):
        return True
    return False


def is_orphan_suffix_only_row(line_words: List[dict]) -> bool:
    """整行仅为上一商品名称续行（无 *类别* 首行、无金额列数值）"""
    row_text = _row_text(line_words)
    if has_project_name_at_start(row_text):
        return False
    text = row_text.replace(' ', '')
    if not text:
        return False
    if is_buyer_seller_fragment_text(text):
        return False
    if DATE_REGEX_LOOSE.search(row_text):
        return False
    if ITEM_NAME_START_PATTERN.search(text):
        return False
    if re.search(r'-?\d+\.\d{2}', text):
        return False
    # 新开括号段（如（GREENER）M35…）是规格折行，走跨行合并而非 orphan 剥离
    if re.search(r'[（(].+[）)]', text) and len(text) > 10:
        return False
    if is_orphan_suffix_fragment_text(text):
        return True
    return False


def extract_orphan_suffix_text(line_words: List[dict]) -> str:
    """从纯续行中提取项目名称折行片段（如「精品)」「钗烤烟)」）"""
    text = _row_text(line_words).replace(' ', '').strip()
    if not text or has_project_name_at_start(text):
        return ''
    return text


def split_orphan_suffix_fragments(
    text: str,
    line_words: Optional[List[dict]] = None,
) -> List[str]:
    """
    将一行内的多个续行 suffix 拆成独立片段。

    混排叠印续行常见「） 冰爵2.0）」：前者属泰山（望岳，后者属万宝路（硬。
    """
    if line_words:
        frags: List[str] = []
        for w in sorted(line_words, key=lambda x: x['x0']):
            part = clean_garbled_chars(w.get('text', '')).strip().replace(' ', '')
            if part and is_orphan_suffix_fragment_text(part):
                frags.append(part)
        if len(frags) > 1:
            return frags
        if len(frags) == 1:
            return frags

    compact = (text or '').replace(' ', '').strip()
    if not compact:
        return []

    parts = re.findall(
        r'(?:[）)]|(?:[\u4e00-\u9fa5A-Za-z（(0-9.]+[）)]))',
        compact,
    )
    frags = [p for p in parts if is_orphan_suffix_fragment_text(p)]
    if len(frags) > 1:
        return frags
    if frags:
        return frags
    if is_orphan_suffix_fragment_text(compact):
        return [compact]
    return []


def pick_orphan_suffix_for_parent(parent_name: str, orphan_text: str) -> str:
    """从可能含多段 suffix 的文本中，选取最适配 parent 的一段（优先最短且能闭合括号）"""
    fragments = split_orphan_suffix_fragments(orphan_text)
    if not fragments:
        frag = (orphan_text or '').strip()
        return frag if suffix_completes_parent_name(parent_name, frag) else ''
    valid = [f for f in fragments if suffix_completes_parent_name(parent_name, f)]
    if not valid:
        return ''
    if len(valid) == 1:
        return valid[0]
    return min(valid, key=lambda s: len(s.replace(' ', '')))


def strip_orphan_prefix_from_name(item_name: str) -> str:
    """去掉项目名称字符串中首个 *类别* 之前的孤儿续行片段"""
    if not item_name:
        return item_name
    compact = item_name.replace(' ', '')
    if has_project_name_at_start(compact):
        return compact
    for i, ch in enumerate(compact):
        if ch != '*':
            continue
        rest = compact[i:]
        if not has_project_name_at_start(rest):
            continue
        prefix = compact[:i]
        if PROJECT_NAME_REGEX.search(prefix):
            return rest
        if re.search(r'[）)]$', prefix) or (
            len(prefix) <= 15
            and re.search(r'[\u4e00-\u9fa5]', prefix)
            and '*' not in prefix
        ):
            return rest
        break
    return item_name.strip()


def estimate_overlay_layer_start_from_duplicate_amounts(
    lines_dict: dict,
    primary_header_y: float,
    min_y_gap: float = 80.0,
) -> Optional[float]:
    """
    表格词集已去掉 B 套表头时，用「同金额商品行 Y 相差较大」估计叠印层起始 Y。
    """
    amount_rows: Dict[str, List[float]] = {}
    for y, line_words in lines_dict.items():
        if y <= primary_header_y + NEXT_HEADER_MARGIN:
            continue
        row_text = _row_text(line_words)
        if not PROJECT_NAME_REGEX.search(row_text):
            continue
        for amt in re.findall(r'\d+\.\d{2}', row_text):
            amount_rows.setdefault(amt, []).append(y)

    overlay_candidates: List[float] = []
    for ys in amount_rows.values():
        if len(ys) < 2:
            continue
        ys_sorted = sorted(set(ys))
        for i in range(len(ys_sorted) - 1):
            gap = ys_sorted[i + 1] - ys_sorted[i]
            if gap >= min_y_gap:
                overlay_candidates.append(ys_sorted[i + 1])

    if not overlay_candidates:
        return None
    return min(overlay_candidates) - 9.0


def resolve_layout_for_table_parse(
    lines_dict: dict,
    layout_from_partition: Optional[LayoutBounds] = None,
) -> Optional[LayoutBounds]:
    """解析表格明细时使用的叠印 layout（保留分区阶段计算的 B 层边界）"""
    layout = layout_from_partition
    if layout is None:
        layout = compute_layout_bounds(lines_dict)
    if layout is None or not layout.has_overlay:
        return None
    if layout.overlay_y_offset is None:
        layout.overlay_y_offset = estimate_overlay_y_offset(lines_dict, layout.primary_header_y)
    if layout.overlay_layer_start_y is None:
        layout.overlay_layer_start_y = estimate_overlay_layer_start_from_duplicate_amounts(
            lines_dict, layout.primary_header_y,
        )
    return layout


def estimate_overlay_y_offset(
    lines_dict: dict,
    primary_header_y: float,
    min_gap: float = 90.0,
    max_gap: float = 400.0,
) -> Optional[float]:
    """
    从同商品 (项目名, 金额) 的两条记录 Y 差估计叠印垂直偏移（常见约 129pt 或 330pt）。
    """
    buckets: Dict[Tuple[str, str], List[float]] = {}
    for y, line_words in lines_dict.items():
        if y <= primary_header_y + NEXT_HEADER_MARGIN:
            continue
        row_text = _row_text(line_words)
        if not PROJECT_NAME_REGEX.search(row_text):
            continue
        amounts = re.findall(r'\d+\.\d{2}', row_text)
        if not amounts:
            continue
        for m in iter_valid_project_name_matches(row_text.replace(' ', '')):
            prefix = m.group(0)[:40]
            key = (_normalize_dedup_name(prefix), amounts[0])
            buckets.setdefault(key, []).append(y)

    gaps: List[float] = []
    for ys in buckets.values():
        if len(ys) < 2:
            continue
        ys_sorted = sorted(set(ys))
        for i in range(len(ys_sorted) - 1):
            gap = ys_sorted[i + 1] - ys_sorted[i]
            if min_gap <= gap <= max_gap:
                gaps.append(gap)
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def overlay_layer_start_y(layout: LayoutBounds) -> Optional[float]:
    """叠印 B 套明细区起始 Y（主层表头下方重复表头再往下）"""
    if layout.overlay_layer_start_y is not None:
        return layout.overlay_layer_start_y
    if not layout.has_overlay or not layout.duplicate_header_ys:
        return None
    return min(layout.duplicate_header_ys) + 9.0


def is_overlay_layer_row(y: float, layout: Optional[LayoutBounds]) -> bool:
    """是否属于叠印 B 套明细区（主层之后的重复商品区）"""
    if layout is None:
        return False
    start_y = overlay_layer_start_y(layout)
    if start_y is None:
        return False
    return y >= start_y


def _is_stacked_x_positions(positions: List[float], tolerance: float = STACKED_X_TOLERANCE) -> bool:
    if len(positions) < 2:
        return False
    return max(positions) - min(positions) <= tolerance


def _split_stacked_name_segments(
    sorted_words: List[dict],
    anchor_indices: List[int],
    n: int,
) -> List[List[dict]]:
    """垂直叠印：多个商品名/条 挤在同一 X，按词序拆成单名单词片段"""
    segments: List[List[dict]] = []
    for si in range(n):
        if si < len(anchor_indices):
            segments.append([sorted_words[anchor_indices[si]]])
    if segments:
        logger.debug('叠印明细行拆分(叠置): %d 个名称片段', len(segments))
    return segments if segments else [sorted_words]


def split_merged_detail_row(line_words: List[dict]) -> List[List[dict]]:
    """
    同一 Y 行叠印了多条商品时，按「*类别*商品名」拆成多段再分别解析。

    1. 名称/条 垂直叠在同一 X → 按词序拆成单名片段（数值列由上层按 index 取）
    2. 否则优先用相邻「条」列 X 中点切分
    3. 否则用商品名锚点 X 中点切分
    """
    if not line_words:
        return []

    sorted_words = sorted(line_words, key=lambda w: w['x0'])
    row_text = _row_text(sorted_words)
    # 必须用「类别含中文」过滤，否则 100mm*45米 会与 *文具*… 组成伪第二商品名
    matches = list(iter_valid_project_name_matches(row_text))
    if len(matches) < 2:
        return [line_words]

    n = len(matches)
    char_pos = 0
    word_ranges: List[Tuple[int, int, int]] = []
    for i, w in enumerate(sorted_words):
        wt = w['text']
        word_ranges.append((char_pos, char_pos + len(wt), i))
        char_pos += len(wt) + 1

    def word_index_at(char_start: int) -> int:
        for start, end, idx in word_ranges:
            if start <= char_start < end:
                return idx
        return word_ranges[-1][2] if word_ranges else 0

    anchor_indices = [word_index_at(m.start()) for m in matches]
    if len(set(anchor_indices)) == 1:
        logger.debug('叠印明细行: 多商品名在同一单词，不在词级拆分')
        return [line_words]

    anchor_xs = [sorted_words[i]['x0'] for i in anchor_indices]
    stacked_names = _is_stacked_x_positions(anchor_xs)

    tiao_words = [
        w for w in sorted_words
        if re.fullmatch(r'条', clean_garbled_chars(w['text']).strip())
    ]
    if len(tiao_words) >= n:
        tiao_centers = [(w['x0'] + w['x1']) / 2 for w in tiao_words[:n]]
        if stacked_names or _is_stacked_x_positions(tiao_centers):
            return _split_stacked_name_segments(sorted_words, anchor_indices, n)
        boundaries = [
            (tiao_centers[i] + tiao_centers[i + 1]) / 2 for i in range(n - 1)
        ]
        segments: List[List[dict]] = [[] for _ in range(n)]
        for w in sorted_words:
            xc = (w['x0'] + w['x1']) / 2
            si = 0
            for i, boundary in enumerate(boundaries):
                if xc >= boundary:
                    si = i + 1
            segments[si].append(w)
        for seg in segments:
            seg.sort(key=lambda w: w['x0'])
        logger.debug('叠印明细行拆分(条列): %d 个商品片段', len(segments))
        return segments

    if stacked_names:
        return _split_stacked_name_segments(sorted_words, anchor_indices, n)

    boundaries = [(anchor_xs[i] + anchor_xs[i + 1]) / 2 for i in range(n - 1)]

    segments = [[] for _ in range(n)]
    assigned: Set[int] = set()

    for si in range(n):
        start_i = anchor_indices[si]
        end_i = anchor_indices[si + 1] if si + 1 < n else len(sorted_words)
        for j in range(start_i, end_i):
            segments[si].append(sorted_words[j])
            assigned.add(j)

    for j, w in enumerate(sorted_words):
        if j in assigned:
            continue
        xc = (w['x0'] + w['x1']) / 2
        si = 0
        for i, boundary in enumerate(boundaries):
            if xc >= boundary:
                si = i + 1
        segments[si].append(w)

    for seg in segments:
        seg.sort(key=lambda w: w['x0'])

    logger.debug('叠印明细行拆分(名称): %d 个商品片段', len(segments))
    return segments


def _normalize_dedup_name(name: str) -> str:
    n = (name or '').replace(' ', '')
    return n.replace('（', '(').replace('）', ')')


def item_dedup_key(item_name: str, amount: str) -> Tuple[str, str]:
    name = _normalize_dedup_name(item_name)
    match = re.match(r'(\*[^*]+\*[^*]+)', name)
    name_key = match.group(1) if match else name[:40]
    amount_parts = (amount or '').replace(' ', '').split()
    amount_key = amount_parts[0] if amount_parts else ''
    return name_key, amount_key


class OverlayItemDedupTracker:
    """
    叠印场景下去重：保留主层（较小 Y）记录。

    判定叠印副本的两条规则（满足其一即丢弃较高 Y）：
    1. 相同 (项目名前缀, 金额) 且 Y 更大；
    2. Y 差 ≈ overlay_y_offset（如约 129pt），说明是垂直平移的 B 套副本。
    """

    def __init__(self, overlay_y_offset: Optional[float] = None) -> None:
        self._offset = overlay_y_offset
        self._seen: Dict[Tuple[str, str], float] = {}
        self._seen_name_only: Dict[str, float] = {}

    def _is_overlay_y_pair(self, y_new: float, y_primary: float) -> bool:
        if self._offset is None:
            return False
        return abs((y_new - y_primary) - self._offset) <= OVERLAY_Y_OFFSET_TOLERANCE

    def should_keep(self, item_name: str, amount: str, y: float, layout: Optional[LayoutBounds]) -> bool:
        key = item_dedup_key(item_name, amount)
        if not key[0]:
            return True

        offset = self._offset
        if layout and layout.overlay_y_offset and offset is None:
            offset = layout.overlay_y_offset

        if not key[1]:
            if (
                layout and layout.has_overlay
                and key[0] in self._seen_name_only
                and not key[0].startswith('*')
            ):
                prev_y = self._seen_name_only[key[0]]
                if offset and self._is_overlay_y_pair(y, prev_y):
                    logger.debug(
                        '叠印去重(偏移%.0f): 跳过续行副本 key=%s y=%.2f (主层y=%.2f)',
                        offset, key[0], y, prev_y,
                    )
                    return False
            if key[0] not in self._seen_name_only or y < self._seen_name_only.get(key[0], float('inf')):
                self._seen_name_only[key[0]] = y
            return True

        if key in self._seen:
            prev_y = self._seen[key]
            if layout and layout.has_overlay:
                if offset and self._is_overlay_y_pair(y, prev_y):
                    keep_y = self._preferred_primary_y(y, prev_y, layout)
                    if abs(y - keep_y) > 5:
                        logger.debug(
                            '叠印去重(偏移%.0f): 跳过 y=%.2f, 保留 y=%.2f key=%s',
                            offset or 0, y, keep_y, key,
                        )
                        return False
                    self._seen[key] = keep_y
                    self._seen_name_only[key[0]] = keep_y
                    return True
                if y < prev_y - 5:
                    self._seen[key] = y
                    self._seen_name_only[key[0]] = y
                    return True
                if y > prev_y + 5:
                    logger.debug(
                        '叠印去重(重复): 跳过 key=%s y=%.2f (主层y=%.2f)',
                        key, y, prev_y,
                    )
                    return False
                return False
            return True

        self._seen[key] = y
        self._seen_name_only[key[0]] = y
        return True

    def _preferred_primary_y(
        self, y1: float, y2: float, layout: Optional[LayoutBounds],
    ) -> float:
        """叠印成对 (y, y+offset) 中主层始终在较小 Y（B 套在下方 +offset 平移）"""
        return min(y1, y2)


def is_buyer_seller_noise_row(row_text: str) -> bool:
    text = row_text.replace(' ', '')
    if PROJECT_NAME_REGEX.search(text):
        return False
    if any(kw in text for kw in BUYER_SELLER_ROW_KEYWORDS):
        return True
    if re.fullmatch(r'[信息购销买卖方\s]+', text):
        return True
    if TAX_ID_WORD_PATTERN.search(text.replace(' ', '')) and '条' not in text and '金额' not in text:
        if not re.search(r'\d+\.\d{2}', text):
            return True
    return False


def extract_row_product_keys(line_words: List[dict]) -> Set[Tuple[str, str]]:
    """从行文本提取 (项目名前缀, 金额) 集合，用于叠印行比对"""
    cleaned = strip_orphan_prefix_words(line_words)
    row_text = _row_text(cleaned).replace(' ', '')
    valid_matches = list(iter_valid_project_name_matches(row_text))
    if not valid_matches:
        return set()
    amounts = re.findall(r'\d+\.\d{2}', row_text)
    keys: Set[Tuple[str, str]] = set()
    for i, m in enumerate(valid_matches):
        name_key = _normalize_dedup_name(m.group(0)[:40])
        match = re.match(r'(\*[^*]+\*[^*]+)', name_key)
        name_key = match.group(1) if match else name_key[:40]
        amount_key = ''
        if amounts:
            amount_key = amounts[i] if i < len(amounts) else amounts[-1]
        keys.add((name_key, amount_key))
    return keys


def score_detail_row_quality(line_words: List[dict], col_x_ranges: Optional[dict]) -> float:
    """明细行解析质量分（越高越像主层完整行）"""
    if not line_words:
        return 0.0
    score = 0.0
    row_text = _row_text(line_words)
    n_products = count_item_name_starts(line_words)
    if n_products == 1:
        score += 4.0
    elif n_products >= 2:
        score -= 3.0
    if is_overlay_contaminated_row(line_words):
        score -= 4.0
    if is_orphan_suffix_only_row(line_words):
        score -= 1.0
    if strip_orphan_prefix_words(line_words) != line_words:
        score -= 2.0
    if col_x_ranges and row_has_item_data_signal(line_words, col_x_ranges):
        score += 2.0
    if re.search(r'\d+\.\d{2}', row_text):
        score += 1.0
    if is_multi_product_merged_row(line_words):
        score -= 2.0
    return score


def _find_line_near_y(lines_dict: dict, target_y: float, tolerance: float = 5.0) -> Optional[List[dict]]:
    for py, pw in lines_dict.items():
        if abs(py - target_y) <= tolerance:
            return pw
    return None


def _product_name_prefixes_overlap(a: str, b: str) -> bool:
    a = _normalize_dedup_name(a)[:20]
    b = _normalize_dedup_name(b)[:20]
    if not a or not b:
        return False
    return a.startswith(b[:10]) or b.startswith(a[:10])


def is_overlay_backward_copy_row(
    y: float,
    line_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    """
    后向叠印副本：y ≈ y_primary + offset，且 y-offset 处已有同名/同金额商品。

    仅用于单行单商品；多商品混排行在片段级处理（避免误丢同行的真实商品）。
    """
    if count_item_name_starts(line_words) >= 2:
        return False
    return _is_overlay_backward_copy_segment(y, line_words, lines_dict, layout, col_x_ranges)


def _is_overlay_backward_copy_segment(
    y: float,
    segment_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    if not layout or not layout.has_overlay or not layout.overlay_y_offset:
        return False
    start = overlay_layer_start_y(layout)
    if start is None or y < start - 5:
        return False
    back_y = y - layout.overlay_y_offset
    back_words = _find_line_near_y(lines_dict, back_y)
    if not back_words or not row_has_item_data_signal(back_words, col_x_ranges or {}):
        return False
    if not PROJECT_NAME_REGEX.search(_row_text(segment_words)):
        return False
    # 仅当当前 Y 在参照行下方（y ≈ back_y + offset）时，当前为叠印副本
    if y <= back_y + 5:
        return False
    back_keys = extract_row_product_keys(back_words)
    cur_keys = extract_row_product_keys(segment_words)
    if back_keys & cur_keys:
        logger.debug(
            '叠印跳过(后向片段): y=%.2f 为 y=%.2f 叠印副本 同键 %s',
            y, back_y, back_keys & cur_keys,
        )
        return True
    back_names = [
        m.group(0) for m in PROJECT_NAME_REGEX.finditer(_row_text(back_words).replace(' ', ''))
    ]
    cur_names = [
        m.group(0) for m in PROJECT_NAME_REGEX.finditer(
            _row_text(strip_orphan_prefix_words(segment_words)).replace(' ', ''),
        )
    ]
    for bn in back_names:
        for cn in cur_names:
            if _product_name_prefixes_overlap(bn, cn):
                logger.debug(
                    '叠印跳过(后向片段): y=%.2f 为 y=%.2f 叠印副本 项目名重叠',
                    y, back_y,
                )
                return True
    return False


def is_overlay_forward_inferior_copy_row(
    y: float,
    line_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    """
    前向叠印副本：y+offset 处有同键商品且质量更高。

    仅用于单行单商品；多商品混排行在片段级处理。
    """
    if count_item_name_starts(line_words) >= 2:
        return False
    return _is_overlay_forward_inferior_copy_segment(
        y, line_words, lines_dict, layout, col_x_ranges,
    )


def _is_overlay_forward_inferior_copy_segment(
    y: float,
    segment_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    """
    前向质量比较已停用：交织区易把主层 (如 Y=195 泰山心悦) 误杀，
    改由后向副本判断 (y > back_y+offset) + 去重保留较小 Y 处理。
    """
    return False


def is_segment_overlay_backward_copy(
    y: float,
    segment_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    """混排行中的单个商品片段是否为后向叠印副本"""
    return _is_overlay_backward_copy_segment(
        y, segment_words, lines_dict, layout, col_x_ranges,
    )


def is_segment_overlay_forward_inferior_copy(
    y: float,
    segment_words: List[dict],
    lines_dict: dict,
    layout: Optional[LayoutBounds],
    col_x_ranges: Optional[dict],
) -> bool:
    """混排行中的单个商品片段是否为前向劣质叠印副本"""
    return _is_overlay_forward_inferior_copy_segment(
        y, segment_words, lines_dict, layout, col_x_ranges,
    )


def _orphan_suffix_core(text: str) -> str:
    s = _normalize_dedup_name((text or '').strip())
    return re.sub(r'[）)\s]+$', '', s)


def _orphan_suffixes_match(a: str, b: str) -> bool:
    """续行后缀是否同一片段（允许叠印层多出的括号/空格，如 钗薄荷） vs 钗薄荷） ））"""
    if not a or not b:
        return False
    na, nb = _normalize_dedup_name(a), _normalize_dedup_name(b)
    if na == nb:
        return True
    ca, cb = _orphan_suffix_core(na), _orphan_suffix_core(nb)
    if ca and cb and ca == cb:
        return True
    if ca and cb and (ca in cb or cb in ca) and min(len(ca), len(cb)) >= 2:
        return True
    return False


def is_overlay_orphan_suffix_duplicate(
    y: float,
    line_words: List[dict],
    layout: Optional[LayoutBounds],
    lines_dict: dict,
) -> bool:
    """续行碎片是否在叠印层重复出现（Y ≈ 主层同文本 + overlay_y_offset）"""
    if not layout or not layout.has_overlay or not layout.overlay_y_offset:
        return False
    if not is_orphan_suffix_only_row(line_words):
        return False
    suffix = _normalize_dedup_name(extract_orphan_suffix_text(line_words))
    if not suffix:
        return False
    offset = layout.overlay_y_offset
    for oy, ow in lines_dict.items():
        if oy >= y - 5:
            continue
        if abs(y - oy - offset) > OVERLAY_Y_OFFSET_TOLERANCE:
            continue
        other = _normalize_dedup_name(extract_orphan_suffix_text(ow))
        if _orphan_suffixes_match(suffix, other):
            logger.debug(
                '叠印跳过: 续行副本 y=%.2f 对应主层 y=%.2f 文本="%s"',
                y, oy, suffix[:20],
            )
            return True
    return False


def is_overlay_orphan_suffix_text_duplicate(
    y: float,
    suffix_text: str,
    lines_dict: dict,
    layout: Optional[LayoutBounds],
) -> bool:
    """根据续行文本判断是否为叠印层重复后缀（用于跨行合并阶段）"""
    if not suffix_text or not suffix_text.strip():
        return False
    fake_words = [{'text': suffix_text.strip(), 'x0': 0.0, 'x1': 0.0}]
    return is_overlay_orphan_suffix_duplicate(
        y, fake_words, layout, lines_dict,
    )


def continuation_y_matches_predecessor(
    predecessor_y: float,
    orphan_y: float,
    layout: Optional[LayoutBounds] = None,
    max_cross_block_gap: float = 900.0,
    allow_cross_block: bool = False,
) -> bool:
    """
    续行 Y 是否紧跟在上一明细行之后。

    叠印仅允许常规行距（及 overlay 偏移）；跨页块大 Y 间距仅用于非叠印多页模版。
    """
    if orphan_suffix_y_matches_parent(orphan_y, predecessor_y, layout):
        return True
    if not allow_cross_block or (layout and layout.has_overlay):
        return False
    gap = orphan_y - predecessor_y
    min_gap = CONTINUATION_LINE_GAP - CONTINUATION_Y_TOLERANCE
    if gap > min_gap and gap <= max_cross_block_gap:
        return True
    return False


def orphan_suffix_y_matches_parent(
    orphan_y: float,
    parent_y: float,
    layout: Optional[LayoutBounds],
) -> bool:
    """
    续行 Y 是否对应 parent 商品行的折行位置。

    - 主层续行: orphan_y ≈ parent_y + line_gap
    - 叠印层续行（主层商品被 B 套挤占下一行时）:
      orphan_y ≈ parent_y + overlay_y_offset + line_gap
    """
    gap = CONTINUATION_LINE_GAP
    tol = CONTINUATION_Y_TOLERANCE
    if abs(orphan_y - parent_y - gap) <= tol:
        return True
    offset = layout.overlay_y_offset if layout and layout.has_overlay else None
    if offset and abs(orphan_y - parent_y - offset - gap) <= tol:
        return True
    return False


def orphan_suffix_owned_by_parent_y(
    orphan_y: float,
    parent_y: float,
    suffix_text: str,
    layout: Optional[LayoutBounds],
    lines_dict: dict,
) -> bool:
    """
    续行 suffix 是否应合并到 parent_y 对应的商品（排除叠印副本误配）。

    例: y=216 的「钗薄荷）」是 y=87 主层续行的叠印副本，不应配 y=207 的泰山（颜悦。
    例: y=504 的「云龙）」是 y=366 云烟（细支 在叠印层的续行，应配 y=366。
    """
    if not orphan_suffix_y_matches_parent(orphan_y, parent_y, layout):
        return False
    gap = CONTINUATION_LINE_GAP
    tol = CONTINUATION_Y_TOLERANCE
    offset = layout.overlay_y_offset if layout and layout.has_overlay else None

    if abs(orphan_y - parent_y - gap) <= tol:
        if offset and is_overlay_orphan_suffix_text_duplicate(
            orphan_y, suffix_text, lines_dict, layout,
        ):
            primary_orphan_y = orphan_y - offset
            expected_parent_y = primary_orphan_y - gap
            return abs(parent_y - expected_parent_y) <= tol + 3
        return True

    return True


def should_skip_table_row(
    y: float,
    line_words: List[dict],
    header_row_y: float,
    layout: Optional[LayoutBounds] = None,
    col_x_ranges: Optional[dict] = None,
    lines_dict: Optional[dict] = None,
) -> bool:
    """判断是否跳过明细噪声行"""
    if abs(y - header_row_y) <= NEXT_HEADER_MARGIN:
        return True

    if is_orphan_suffix_only_row(line_words):
        if lines_dict and is_overlay_orphan_suffix_duplicate(y, line_words, layout, lines_dict):
            return True
        return False

    row_text = _row_text(line_words)
    if is_table_header_row(line_words) and abs(y - header_row_y) > NEXT_HEADER_MARGIN:
        return True

    if layout and layout.has_overlay and lines_dict:
        if is_overlay_backward_copy_row(y, line_words, lines_dict, layout, col_x_ranges):
            return True
        if is_overlay_forward_inferior_copy_row(y, line_words, lines_dict, layout, col_x_ranges):
            return True

    if layout and layout.has_overlay:
        for dup_y in layout.duplicate_header_ys:
            if (
                abs(y - dup_y) <= NEXT_HEADER_MARGIN
                and abs(y - header_row_y) > NEXT_HEADER_MARGIN
                and is_table_header_row(line_words)
            ):
                return True

    if is_invoice_header_noise_row(line_words):
        return True

    if is_summary_row_text(row_text):
        return True

    if is_buyer_seller_noise_row(row_text):
        return True

    # 叠印 B 层与主层 Y 交错，不能用 Y 阈值整段跳过；靠 (项目名, 金额) 去重保留较小 Y

    if layout and layout.has_overlay and is_multi_product_merged_row(line_words):
        filtered_for_split = (
            filter_row_noise_words(line_words, col_x_ranges) if col_x_ranges else line_words
        )
        if len(split_merged_detail_row(filtered_for_split)) < 2:
            logger.debug('叠印跳过: 行Y=%.2f 含多个商品名且无法拆分', y)
            return True

    if col_x_ranges:
        filtered = filter_row_noise_words(line_words, col_x_ranges)
        if not filtered:
            return True
        if not row_has_item_data_signal(filtered, col_x_ranges):
            if is_overlay_contaminated_row(line_words):
                return True
            if layout and layout.has_overlay and count_item_name_starts(line_words) >= 1:
                if not row_has_item_data_signal(line_words, col_x_ranges):
                    return True

    return False


def _assign_word_column(word: dict, col_x_ranges: dict) -> Optional[str]:
    """将单词分配到最匹配的表头列"""
    center = (word['x0'] + word['x1']) / 2
    best_col = None
    best_dist = float('inf')
    for keyword, (x0, x1) in col_x_ranges.items():
        if x0 <= center <= x1:
            col_center = (x0 + x1) / 2
            dist = abs(center - col_center)
            if dist < best_dist:
                best_dist = dist
                best_col = keyword
    return best_col


def _is_company_or_buyer_text(text: str) -> bool:
    if PROJECT_NAME_REGEX.search(text):
        return False
    if re.match(r'^名称[：:]$', text.strip()):
        return True
    if '统一社会信用代码' in text or '纳税人识别号' in text:
        return True
    if COMPANY_NAME_PATTERN.search(text):
        return True
    return False


def _always_drop_word(text: str) -> bool:
    t = clean_garbled_chars(text).strip()
    if not t:
        return True
    if t in ALWAYS_DROP_EXACT:
        return True
    for kw in WORD_NOISE_KEYWORDS:
        if kw in t:
            return True
    for kw in INVOICE_HEADER_WORDS:
        if kw in t:
            return True
    normalized = t.replace(' ', '')
    if TAX_ID_WORD_PATTERN.match(normalized):
        return True
    if re.search(r'共\d*页|第\d*页', normalized):
        return True
    return False


def _is_valid_column_word(column: str, text: str) -> bool:
    t = clean_garbled_chars(text).strip()
    if not t:
        return False
    if _always_drop_word(t):
        return False
    if _is_company_or_buyer_text(t):
        return column == '项目名称' and bool(PROJECT_NAME_REGEX.search(t))
    if column in {'数量', '单价', '金额', '税额'}:
        return bool(NUMERIC_COLUMN_PATTERN.match(t.replace(' ', '')))
    if column == '税率':
        return bool(NUMERIC_COLUMN_PATTERN.match(t.replace(' ', '')))
    if column == '单位':
        return len(t) <= 6 and not _is_company_or_buyer_text(t)
    if column == '规格型号':
        return not _is_company_or_buyer_text(t)
    if column == '项目名称':
        if re.match(r'^名称[：:]$', t):
            return False
        return bool(PROJECT_NAME_REGEX.search(t)) or (
            re.search(r'[\u4e00-\u9fa5]', t) and not _is_company_or_buyer_text(t)
        )
    return True


def is_overlay_contaminated_row(line_words: List[dict]) -> bool:
    """购销方叠印进明细行：同时含商品名与购销方关键词"""
    row_text = _row_text(line_words)
    if not PROJECT_NAME_REGEX.search(row_text):
        return False
    text = row_text.replace(' ', '')
    if any(kw in text for kw in BUYER_SELLER_ROW_KEYWORDS):
        return True
    if COMPANY_NAME_PATTERN.search(row_text) and '条' in row_text:
        return True
    return False


def row_has_item_data_signal(line_words: List[dict], col_x_ranges: dict) -> bool:
    """行内是否有有效明细信号（项目名称或金额/数量列有合法值）"""
    if is_orphan_suffix_only_row(line_words):
        return True

    data_columns = {'项目名称', '规格型号', '金额', '数量', '单价'}
    item_name_x0 = col_x_ranges.get('项目名称', (None, None))[0]
    for word in line_words:
        text = clean_garbled_chars(word.get('text', '')).strip()
        if not text or _always_drop_word(text):
            continue
        col = _assign_word_column(word, col_x_ranges)
        if col is None and item_name_x0 is not None:
            word_center = (word['x0'] + word['x1']) / 2
            if word_center < item_name_x0 and _is_valid_column_word('项目名称', text):
                return True
        if col not in data_columns:
            continue
        if _is_valid_column_word(col, text):
            return True
    return False


def _word_in_any_column(word: dict, col_x_ranges: dict) -> bool:
    return _assign_word_column(word, col_x_ranges) is not None


def _is_noise_word(word_text: str, in_column: bool) -> bool:
    text = clean_garbled_chars(word_text).strip()
    if _always_drop_word(text):
        return True
    if text in BUYER_SELLER_FRAGMENT_KEYWORDS and not in_column:
        return True
    for kw in WORD_NOISE_KEYWORDS:
        if kw in text and not in_column:
            return True
    normalized = text.replace(' ', '')
    if TAX_ID_WORD_PATTERN.match(normalized) and not in_column:
        return True
    if not in_column and _is_company_or_buyer_text(text):
        return True
    return False


def filter_row_noise_words(line_words: List[dict], col_x_ranges: dict) -> List[dict]:
    """基于表头列范围，过滤行内购销方/发票头等噪声词"""
    if not col_x_ranges:
        return line_words

    kept = []
    for word in line_words:
        text = clean_garbled_chars(word.get('text', ''))
        if _always_drop_word(text):
            continue
        col = _assign_word_column(word, col_x_ranges)
        if col is not None:
            if not _is_valid_column_word(col, text):
                continue
        elif _is_noise_word(text, False):
            continue
        kept.append(word)
    return kept


def clean_extracted_item_name(item_name: str) -> str:
    """清理提取后的项目名称（去掉叠印混入的购销方片段）"""
    if not item_name:
        return item_name
    name = re.sub(r'名称[：:].*', '', item_name).strip()
    name = re.sub(r'\s+名称[：:]\s*', '', name).strip()
    name = strip_orphan_prefix_from_name(name)
    if _is_company_or_buyer_text(name) and not PROJECT_NAME_REGEX.search(name):
        return ''
    return name


def find_primary_header_row_y(
    sorted_lines: List[Tuple[float, List[dict]]],
    layout: Optional[LayoutBounds] = None,
) -> Optional[float]:
    """在表格行列表中查找主层表头 Y"""
    if layout:
        for y, line_words in sorted_lines:
            if abs(y - layout.primary_header_y) <= NEXT_HEADER_MARGIN and is_table_header_row(line_words):
                return y

    for y, line_words in sorted_lines:
        if is_table_header_row(line_words):
            return y
    return None
