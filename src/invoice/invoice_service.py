"""
发票解析主服务
"""
import os
import logging
import re
from typing import List, Optional, Tuple
import pdfplumber
import invoice_const

from .invoice import Invoice, LineItem
from .regex_utils import (
    INVOICE_CODE_REGEX, INVOICE_CODE_REGEX_LOOSE, DATE_REGEX, DATE_REGEX_LOOSE,
    PROJECT_NAME_REGEX, TAX_ID_PATTERNS, TAX_ID_GLOBAL_REGEX, LONG_NUMBER_REGEX,
    ROOM_NUMBER_REGEX, ROOM_ALPHA_REGEX, COMPANY_NAME_REGEX, normalize_alpha_num,
    has_project_name_at_start,
)
from .text_utils import clean_garbled_chars, reconstruct_text_from_words, normalize_text_whitespace
from .number_utils import (
    is_tax_id,
    score_candidate,
    split_quantity_prefix_from_price,
    amount_matches_qty_price,
    pick_price_matching_amount,
)
from .invoice_partition import (
    partition_invoice_by_lines,
    print_partition_content,
    group_words_by_y
)
from .invoice_layout import (
    find_primary_header_row_y,
    should_skip_table_row,
    filter_row_noise_words,
    clean_extracted_item_name,
    split_merged_detail_row,
    strip_orphan_prefix_words,
    extract_orphan_prefix_from_row,
    is_orphan_suffix_fragment_text,
    split_mixed_row_orphan_suffix,
    peel_orphan_suffix_words_from_mixed_row,
    extract_orphan_suffix_for_previous_row,
    is_orphan_suffix_only_row,
    extract_orphan_suffix_text,
    split_orphan_suffix_fragments,
    pick_orphan_suffix_for_parent,
    is_buyer_seller_fragment_text,
    OverlayItemDedupTracker,
    resolve_layout_for_table_parse,
    LayoutBounds,
    is_segment_overlay_backward_copy,
    is_segment_overlay_forward_inferior_copy,
    is_overlay_orphan_suffix_text_duplicate,
    orphan_suffix_owned_by_parent_y,
    continuation_y_matches_predecessor,
    suffix_completes_parent_name,
    close_item_name_if_paren_only,
    merge_item_name_suffix,
)

# 获取模块级别的日志记录器
logger = logging.getLogger(__name__)

def extract_invoice_by_table_and_text(pdf_file_path: str) -> Invoice:
    """
    使用 pdfplumber 提取PDF中的发票信息（支持多页发票）
    
    Args:
        pdf_file_path: PDF 文件路径
        
    Returns:
        Invoice 对象
        
    Raises:
        Exception: 解析失败
    """
    logging.info(f'开始处理发票 {pdf_file_path}...')

    invoice = Invoice()
    invoice.name = os.path.basename(pdf_file_path)

    try:
        with pdfplumber.open(pdf_file_path) as pdf:
            total_pages = len(pdf.pages)
            logging.info(f'发票共有 {total_pages} 页')

            # 存储第一页的分区结果（用于提取基本信息和列坐标）
            first_page_header_words = None
            first_page_buyer_words = None
            first_page_table_words = None
            first_page_layout = None
            all_table_words = []
            
            # 存储所有页面的 raw_items（未合并的明细项）
            all_raw_items = []

            # 遍历所有页面进行处理
            for page_idx, page in enumerate(pdf.pages):
                logging.info(f'处理第 {page_idx + 1}/{total_pages} 页...')

                # 1. 提取文本内容（过滤掉印章红色文字）
                words = page.extract_words(
                    x_tolerance=5,  # 增大水平容差，帮助合并同一行的字符
                    y_tolerance=2,
                    keep_blank_chars=False,
                    use_text_flow=True  # 使用文本流逻辑重组，解决乱序
                )

                # 过滤掉印章文字（红色）
                clean_words = filter_seal_text(page, words)

                # 2. 使用分割线进行发票分区（对每页都进行分区）
                logging.debug(f'开始使用分割线对第 {page_idx + 1} 页进行发票分区...')
                header_words, buyer_words, seller_words, table_words, page_layout = partition_invoice_by_lines(
                    page, clean_words
                )
                
                # 如果是第一页，保存分区结果用于提取基本信息和列坐标，并打印调试信息
                if page_idx == 0:
                    first_page_header_words = header_words
                    first_page_buyer_words = buyer_words
                    first_page_table_words = table_words
                    first_page_layout = page_layout
                    # 只有在DEBUG日志级别时才打印各分区内容（调试用）
                    if logger.isEnabledFor(logging.DEBUG):
                        print_partition_content(header_words, buyer_words, seller_words, table_words)
                else:
                    logger.debug(f'第 {page_idx + 1} 页分区结果: 发票头={len(header_words)}个单词, '
                               f'购买方={len(buyer_words)}个单词, '
                               f'销售方={len(seller_words)}个单词, '
                               f'表格={len(table_words)}个单词')

                # 从当前页面的 table_words 提取 raw_items（不进行跨行合并）
                if table_words:
                    all_table_words.extend(table_words)
                    page_raw_items = parse_line_items_from_words_raw(
                        table_words, pdf_file_path, page_idx + 1, layout_from_partition=page_layout
                    )
                    if page_raw_items:
                        all_raw_items.extend(page_raw_items)
                        logging.debug(f'第 {page_idx + 1} 页提取到 {len(page_raw_items)} 条原始明细项')

            # 3. 提取发票基本信息（只使用第一页的分区结果）
            if first_page_header_words is not None and first_page_buyer_words is not None:
                code, date, buyer, buyer_tax_id, invoice_type = extract_basic_info(
                    first_page_header_words, first_page_buyer_words
                )

                invoice.code = code
                invoice.date = date
                invoice.buyer = buyer
                invoice.buyer_tax_id = buyer_tax_id
                invoice.invoice_type = invoice_type
            else:
                logger.warning(f'未能从第一页提取到分区数据，基本信息可能为空')

            # 4. 解析商品明细（合并所有页面的 raw_items，然后进行跨行商品记录合并）
            if all_raw_items:
                logging.debug(f'合并所有页面，共 {len(all_raw_items)} 条原始明细项')
                # 需要获取列坐标信息用于跨行合并（使用第一页的表头信息）
                if first_page_table_words:
                    # 从第一页的 table_words 中识别列坐标（使用第一页的表头）
                    col_x_starts = _extract_column_starts_from_table_words(
                        first_page_table_words, pdf_file_path
                    )
                    merge_lines_dict = group_words_by_y(all_table_words, y_tolerance=3.0)
                    merge_layout = resolve_layout_for_table_parse(
                        merge_lines_dict, first_page_layout,
                    )
                    invoice.items = merge_split_line_items(
                        all_raw_items, col_x_starts, pdf_file_path,
                        layout=merge_layout,
                        lines_dict=merge_lines_dict,
                    )
                else:
                    # 如果没有第一页的 table_words，直接使用 raw_items（不进行跨行合并）
                    logger.warning(f'未能从第一页提取到表格数据，跳过跨行合并')
                    invoice.items = all_raw_items
            else:
                # 如果没有 raw_items，尝试使用传统方法解析（兼容处理，仅处理第一页）
                logger.warning(f'未能提取到原始明细项，尝试使用传统方法解析第一页')
                if first_page_table_words:
                    invoice.items = parse_line_items_from_words(first_page_table_words, pdf_file_path)
                else:
                    invoice.items = []

            # 验证解析结果
            if not invoice.items:
                raise Exception('未能解析到发票明细数据')

            logging.info(f'发票 {pdf_file_path} 解析完成，共 {len(invoice.items)} 条明细')
            return invoice

    except Exception as e:
        logger.error(f'读取发票 {pdf_file_path} 发生错误: {e}')
        raise


def filter_seal_text(page, words: List[dict]) -> List[dict]:
    """
    过滤掉印章文字（红色）
    
    特殊处理：即使文字是红色，如果包含"增值税"或"普通发票"关键字，也不过滤
    
    Args:
        page: pdfplumber Page 对象
        words: 提取的单词列表
        
    Returns:
        过滤后的单词列表
    """
    clean_words = []
    chars = page.chars  # 获取所有字符
    
    # 需要保留的关键字（即使红色也不过滤）
    protected_keywords = ['增值税', '普通发票']

    for w in words:
        # 检查单词文本是否包含需要保护的关键字
        word_text = w.get('text', '')
        is_protected = any(keyword in word_text for keyword in protected_keywords)
        
        # 如果包含保护关键字，直接保留，不检查颜色
        if is_protected:
            clean_words.append(w)
            continue
        
        # 找到该词对应的字符，通过位置匹配
        word_chars = []
        for char in chars:
            # 检查字符是否在词的边界框内
            if (char['x0'] >= w['x0'] - 1 and char['x1'] <= w['x1'] + 1 and
                    abs(char['top'] - w['top']) < 3):
                word_chars.append(char)

        # 如果找不到匹配字符，默认保留（可能是特殊情况）
        if not word_chars:
            clean_words.append(w)
            continue

        # 检查该词的所有字符，如果任何一个字符是红色，则过滤掉
        is_red = False
        for char in word_chars:
            color = char.get('non_stroking_color')
            stroke_color = char.get('stroking_color')

            # 检查非描边颜色（填充色）
            if color:
                if len(color) == 3:  # RGB
                    # 红色判断：R值高，G和B值低
                    if color[0] > 0.6 and color[1] < 0.4 and color[2] < 0.4:
                        is_red = True
                        break
                elif len(color) == 4:  # CMYK
                    # CMYK中红色：C低，M高，Y高，K低
                    if color[0] < 0.2 and color[1] > 0.5:
                        is_red = True
                        break

            # 检查描边颜色
            if stroke_color:
                if len(stroke_color) == 3:  # RGB
                    if stroke_color[0] > 0.6 and stroke_color[1] < 0.4 and stroke_color[2] < 0.4:
                        is_red = True
                        break
                elif len(stroke_color) == 4:  # CMYK
                    if stroke_color[0] < 0.2 and stroke_color[1] > 0.5:
                        is_red = True
                        break

        if not is_red:
            clean_words.append(w)

    return clean_words


def extract_basic_info(
    header_words: List[dict],
    buyer_words: List[dict]
) -> Tuple[str, str, str, str, str]:
    """
    从分区后的内容中提取发票基本信息
    
    Args:
        header_words: 发票头区域的单词列表
        buyer_words: 购买方区域的单词列表
        
    Returns:
        (code, date, buyer, buyer_tax_id, invoice_type)
    """
    # 1. 提取发票号码（从 header_words）
    code = extract_invoice_code_from_words(header_words)

    # 2. 提取开票日期（从 header_words）
    date = extract_date_from_words(header_words)

    # 3. 提取购买方名称（从 buyer_words）
    buyer = extract_buyer_name_from_words(buyer_words)

    # 4. 提取购买方税号（从 buyer_words）
    buyer_tax_id = extract_buyer_tax_id_from_words(buyer_words)

    # 5. 提取发票类型（从 header_words）
    invoice_type = extract_invoice_type_from_words(header_words)

    return code, date, buyer, buyer_tax_id, invoice_type


def extract_invoice_code_from_words(header_words: List[dict]) -> str:
    """从发票头区域提取发票号码"""
    if not header_words:
        return ''
    
    # 重建文本
    header_text = reconstruct_text_from_words(header_words)
    # 基本清洗（去除乱码字符）
    header_text = clean_garbled_chars(header_text)
    
    # 优先使用严格模式
    match = INVOICE_CODE_REGEX.search(header_text.replace(' ', ''))
    if match:
        return match.group(1)

    # 回退：使用宽松模式
    match = INVOICE_CODE_REGEX_LOOSE.search(header_text)
    if match:
        return match.group(1)

    # 最后回退：查找所有长数字
    nums = LONG_NUMBER_REGEX.findall(header_text.replace(' ', ''))
    if nums:
        # 选择最长的 12-20 位数字
        valid_nums = [n for n in nums if 12 <= len(n) <= 20]
        if valid_nums:
            return max(valid_nums, key=len)

    return ''


def extract_date_from_words(header_words: List[dict]) -> str:
    """从发票头区域提取开票日期"""
    if not header_words:
        return ''
    
    # 重建文本
    header_text = reconstruct_text_from_words(header_words)
    # 基本清洗
    header_text = clean_garbled_chars(header_text)
    
    # 优先使用严格模式
    match = DATE_REGEX.search(header_text)
    if match:
        return match.group(1)

    # 回退：使用宽松模式
    match = DATE_REGEX_LOOSE.search(header_text)
    if match:
        return match.group().replace(' ', '')

    return ''


_BUYER_NAME_STOP_KEYWORDS = (
    '统一社会信用代码', '纳税人识别号', '销售方', '购买方', '买方', '卖方', '信息',
    '名称：', '名称:',
)

# 「公司」之后若仅为这些组织后缀，视为名称一部分（如 …有限责任公司工会）
_BUYER_NAME_SUFFIX_AFTER_COMPANY = frozenset({
    '工会', '分公司', '支公司', '营业部', '经营部', '服务部', '办事处', '代表处',
})


def _strip_buyer_name_at_stop_keywords(name: str) -> str:
    """截断到噪声关键词之前"""
    buyer = name.strip()
    for kw in _BUYER_NAME_STOP_KEYWORDS:
        if kw in buyer:
            idx = buyer.index(kw)
            if idx > 0:
                return buyer[:idx].strip()
    return buyer


def _trim_buyer_name_at_company_boundary(name: str) -> str:
    """
    仅在「公司」后接销售方/第二段名称等噪声时截到「公司」；
    保留 …有限责任公司工会 等合法后缀。
    """
    if '公司' not in name:
        return name
    idx = name.rindex('公司')
    tail = name[idx + 2:].strip()
    if not tail:
        return name
    if tail in _BUYER_NAME_SUFFIX_AFTER_COMPANY:
        return name
    if re.match(r'^[\u4e00-\u9fa5]{2,8}$', tail) and not any(
        kw in tail for kw in ('销售', '购买', '名称', '公司')
    ):
        return name
    noise_in_tail = any(kw in tail for kw in (
        '名称', '统一社会', '纳税人', '销售', '购买', '买方', '卖方', '公司',
    ))
    if noise_in_tail:
        return name[:idx + 2].strip()
    return name


def _finalize_extracted_buyer_name(name: str) -> str:
    """购买方名称提取后的统一清洗"""
    buyer = normalize_text_whitespace(name.strip())
    buyer = _strip_buyer_name_at_stop_keywords(buyer)
    buyer = _trim_buyer_name_at_company_boundary(buyer)
    return buyer


def extract_buyer_name_from_words(buyer_words: List[dict]) -> str:
    """从购买方区域提取购买方名称"""
    if not buyer_words:
        return ''
    
    # 重建文本
    buyer_text = reconstruct_text_from_words(buyer_words)
    # 基本清洗
    buyer_text = clean_garbled_chars(buyer_text)
    
    # 策略1：从"名称："后面提取，直到遇到"统一社会信用代码"或"纳税人识别号"
    # 使用正向先行断言，匹配"名称："后面到"统一社会信用代码"/"纳税人识别号"之前的内容
    # 使用非贪婪匹配，但确保能匹配到完整内容
    name_pattern = re.compile(r'名称[：:]\s*((?:(?!统一社会信用代码|纳税人识别号).)+?)(?=\s*(?:统一社会信用代码|纳税人识别号)|$)', re.DOTALL)
    match = name_pattern.search(buyer_text)
    if match:
        buyer = _finalize_extracted_buyer_name(match.group(1))
        if buyer and not is_tax_id(buyer) and not re.match(r'^\d{12,}$', buyer):
            return buyer

    # 策略2：匹配公司名称模式
    match = COMPANY_NAME_REGEX.search(buyer_text)
    if match:
        buyer = _finalize_extracted_buyer_name(match.group(1))
        if buyer:
            return buyer

    # 策略3：匹配房间号
    match = ROOM_NUMBER_REGEX.search(buyer_text)
    if match:
        return match.group()

    match = ROOM_ALPHA_REGEX.search(buyer_text)
    if match:
        return match.group()

    # 策略4：查找最长的中文文本（排除税号等）
    cleaned = re.sub(r'\d{12,}', '', buyer_text)
    chinese_texts = re.findall(r'[\u4e00-\u9fa5]{3,}', cleaned)
    if chinese_texts:
        # 排除包含排除关键词的
        exclude_keywords = ['统一社会信用代码', '纳税人识别号', '购买方', '销售方']
        valid_texts = [t for t in chinese_texts if not any(kw in t for kw in exclude_keywords)]
        if valid_texts:
            return max(valid_texts, key=len)

    return ''


def extract_buyer_tax_id_from_words(buyer_words: List[dict]) -> str:
    """从购买方区域提取统一社会信用代码/纳税人识别号"""
    if not buyer_words:
        return ''
    
    # 重建文本
    buyer_text = reconstruct_text_from_words(buyer_words)
    # 基本清洗
    buyer_text = clean_garbled_chars(buyer_text)
    
    # 收集候选税号
    candidates = []

    # 1. 从模式匹配中收集
    for pattern in TAX_ID_PATTERNS:
        matches = pattern.findall(buyer_text)
        candidates.extend(matches)

    # 2. 全局扫描（去空格后）
    buyer_norm = normalize_alpha_num(buyer_text)
    global_matches = TAX_ID_GLOBAL_REGEX.findall(buyer_norm)
    candidates.extend(global_matches)

    # 3. 验证和评分
    valid_candidates = []
    for cand in candidates:
        cand_norm = normalize_alpha_num(cand)
        if 15 <= len(cand_norm) <= 20:
            # 如果是19位，尝试去头/去尾得到18位
            try_list = [cand_norm]
            if len(cand_norm) == 19:
                try_list.extend([cand_norm[1:], cand_norm[:-1]])

            for c in try_list:
                if 15 <= len(c) <= 20 and is_tax_id(c):
                    valid_candidates.append((c, score_candidate(c)))
                    break
                elif 15 <= len(c) <= 20:
                    valid_candidates.append((c, score_candidate(c)))

    # 4. 选择得分最高的
    if valid_candidates:
        valid_candidates.sort(key=lambda x: x[1], reverse=True)
        return valid_candidates[0][0]

    return ''


def extract_invoice_type_from_words(header_words: List[dict]) -> str:
    """从发票头区域提取发票类型"""
    if not header_words:
        return ''
    
    # 重建文本
    header_text = reconstruct_text_from_words(header_words)
    # 基本清洗
    header_text = clean_garbled_chars(header_text)
    
    # 优先识别完整格式
    if '（普通发票）' in header_text or '(普通发票)' in header_text:
        return '普'
    if '（增值税专用发票）' in header_text or '(增值税专用发票)' in header_text:
        return '专'

    # 如果没找到完整格式，尝试查找关键词
    has_putong = '普通' in header_text or '普' in header_text
    has_fapiao = '发票' in header_text
    if has_putong and has_fapiao:
        return '普'
    if '增值税专用发票' in header_text or '专用发票' in header_text:
        return '专'
    if '增值税' in header_text and '专用' in header_text and has_fapiao:
        return '专'

    return ''


def _find_header_row(sorted_lines: List[Tuple[float, List[dict]]], pdf_path: str = '') -> Optional[float]:
    """
    查找表头行
    
    由于输入已经是分离出来的发票明细数据，第一行就是表头行，
    直接检查第一行是否包含足够的表头关键词进行校验。
    
    Args:
        sorted_lines: 按Y坐标排序的行列表
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        表头行的Y坐标，如果未找到返回None
    """
    if not sorted_lines:
        logger.warning(f'{pdf_path}: 表格数据为空')
        return None
    
    # 直接检查第一行（表头行）
    y, line_words = sorted_lines[0]
    row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])])
    
    # 查找表头关键词
    header_text_no_space = row_text.replace(' ', '')
    keyword_count = sum(1 for kw in invoice_const.TABLE_KEYWORDS if kw in header_text_no_space)
    if keyword_count >= invoice_const.MIN_HEADER_KEYWORDS:
        logger.debug(f'{pdf_path}: 找到表头行，Y={y:.2f}, 内容: {row_text[:100]}')
        return y
    
    # 如果第一行不符合，尝试查找其他行（兼容处理）
    logger.warning(f'{pdf_path}: 第一行不符合表头要求（包含{keyword_count}个关键词，需要至少{invoice_const.MIN_HEADER_KEYWORDS}个），尝试查找其他行')
    for y, line_words in sorted_lines[1:]:
        row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])])
        keyword_count = sum(1 for kw in invoice_const.TABLE_KEYWORDS if kw in row_text)
        if keyword_count >= invoice_const.MIN_HEADER_KEYWORDS:
            logger.debug(f'{pdf_path}: 找到表头行，Y={y:.2f}, 内容: {row_text[:100]}')
            return y
    
    logger.warning(f'{pdf_path}: 未找到表头行（需要包含至少{invoice_const.MIN_HEADER_KEYWORDS}个表头关键词）')
    # 打印前几行内容帮助调试
    for i, (y, line_words) in enumerate(sorted_lines[:5]):
        row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])])
        logger.debug(f'{pdf_path}: 表格第 {i} 行 (Y={y:.2f}): {row_text[:100]}')
    return None


def _match_keywords_to_words(header_line_words: List[dict], pdf_path: str = '') -> dict:
    """
    匹配表头关键词到对应的单词
    
    使用两种策略：
    1. 直接匹配：关键词完整出现在单词中
    2. 拆分匹配：处理被拆分的关键词（如"单位"被拆成"单"和"位"）
    
    Args:
        header_line_words: 表头行的单词列表（已按x0排序）
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        {keyword: [word1, word2, ...]} 字典
    """
    keyword_words = {}  # {keyword: [word1, word2, ...]}
    
    # 打印表头行的x0坐标信息
    logger.debug(f'{pdf_path}: ================================================================================')
    logger.debug(f'{pdf_path}: 表头行 X坐标信息:')
    logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
    for word in header_line_words:
        word_text = clean_garbled_chars(word['text'])
        logger.debug(f'{pdf_path}:   单词: "{word_text:20s}" | x0={word["x0"]:8.2f} | x1={word["x1"]:8.2f} | 中心={((word["x0"]+word["x1"])/2):8.2f}')
    
    # 方法1：直接匹配（关键词完整出现在单词中）
    for word in header_line_words:
        word_text = clean_garbled_chars(word['text'])
        for keyword in invoice_const.TABLE_KEYWORDS:
            if keyword in word_text:
                if keyword not in keyword_words:
                    keyword_words[keyword] = []
                keyword_words[keyword].append(word)
    
    # 方法2：处理被拆分的关键词（如"单位"被拆成"单"和"位"）
    for keyword in invoice_const.TABLE_KEYWORDS:
        if keyword in keyword_words:
            continue  # 已经找到完整匹配，跳过
        
        # 找到所有包含关键词中任意字符的单词
        candidate_words = []
        keyword_chars = set(keyword)
        for word in header_line_words:
            word_text = clean_garbled_chars(word['text'])
            # 检查单词是否包含关键词中的字符
            if any(char in word_text for char in keyword_chars):
                candidate_words.append(word)
        
        if not candidate_words:
            continue
        
        # 根据X坐标连续性判断：如果多个单词的X坐标连续且包含关键词的所有字符，则认为是同一列
        # 对于2字符关键词（如"单位"、"数量"、"单价"、"金额"、"税额"），检查是否有2个相邻单词分别包含这2个字符
        if len(keyword) == 2:
            # 找到包含第一个字符和第二个字符的单词
            char1_words = [w for w in candidate_words if keyword[0] in clean_garbled_chars(w['text'])]
            char2_words = [w for w in candidate_words if keyword[1] in clean_garbled_chars(w['text'])]
            
            # 检查是否有相邻的单词对
            for w1 in char1_words:
                for w2 in char2_words:
                    if w1 == w2:
                        continue
                    # 检查X坐标是否相邻（距离小于阈值）
                    distance = abs(w2['x0'] - w1['x1'])
                    if distance < invoice_const.WORD_DISTANCE_THRESHOLD:
                        # 找到匹配的单词对
                        keyword_words[keyword] = [w1, w2]
                        logger.debug(f'{pdf_path}:   列"{keyword}": 通过字符匹配找到相邻单词对: "{clean_garbled_chars(w1["text"])}" (x0={w1["x0"]:.2f}) + "{clean_garbled_chars(w2["text"])}" (x0={w2["x0"]:.2f})')
                        break
                # 如果已经找到匹配的单词对，跳出外层循环
                if keyword in keyword_words:
                    break
        elif len(keyword) == 1:
            # 单字符关键词，直接使用包含该字符的单词
            if candidate_words:
                keyword_words[keyword] = candidate_words[:1]  # 取第一个匹配的单词
    
    return keyword_words


def _calculate_column_ranges(keyword_words: dict, pdf_path: str = '') -> Tuple[dict, dict]:
    """
    计算各列的X坐标范围
    
    Args:
        keyword_words: {keyword: [word1, word2, ...]} 字典
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        (col_x_ranges, col_x_starts) 元组
        - col_x_ranges: {keyword: (x0, x1)} 列的X坐标范围
        - col_x_starts: {keyword: x0} 列的首字x0坐标
    """
    col_x_ranges = {}
    col_x_starts = {}  # 记录每列的首字x0坐标（用于匹配明细数据）
    
    for keyword, words in keyword_words.items():
        if not words:
            continue
        # 找到该关键词所有单词的最小x0和最大x1
        min_x0 = min(w['x0'] for w in words)
        max_x1 = max(w['x1'] for w in words)
        # 扩展范围：左右各扩展容差，以便匹配同一列的其他单词
        col_x_ranges[keyword] = (min_x0 - invoice_const.COLUMN_X_TOLERANCE, max_x1 + invoice_const.COLUMN_X_TOLERANCE)
        col_x_starts[keyword] = min_x0  # 记录首字x0坐标
        logger.debug(f'{pdf_path}:   列"{keyword}": 包含{len(words)}个单词, x0范围=[{min_x0:.2f}, {max_x1:.2f}], 扩展后=[{min_x0-invoice_const.COLUMN_X_TOLERANCE:.2f}, {max_x1+invoice_const.COLUMN_X_TOLERANCE:.2f}]')
    
    logger.debug(f'{pdf_path}: ================================================================================')
    
    return col_x_ranges, col_x_starts


def _line_item_has_numeric_data(item: LineItem) -> bool:
    """行内是否已解析出数量/单价/金额/税率/税额等数值字段"""
    return bool(
        (item.amount and item.amount.strip())
        or (item.quantity and item.quantity.strip())
        or (item.price and item.price.strip())
        or (item.tax_rate and item.tax_rate.strip())
        or (item.tax_amount and item.tax_amount.strip())
    )


def _match_words_in_column(
    col_x_ranges: dict,
    sorted_line_words: List[dict],
    keyword: str,
) -> List[dict]:
    """返回指定列中匹配到的所有单词（按 x0 排序前）。"""
    if keyword not in col_x_ranges:
        return []
    col_x0, col_x1 = col_x_ranges[keyword]
    col_center = (col_x0 + col_x1) / 2

    matched_words = []
    for word in sorted_line_words:
        word_center = (word['x0'] + word['x1']) / 2

        is_item_name_column = (keyword == '项目名称')
        if is_item_name_column:
            if word_center < col_x0:
                matched_other = False
                for other_keyword, (other_x0, other_x1) in col_x_ranges.items():
                    if other_keyword == keyword:
                        continue
                    if other_x0 <= word_center <= other_x1:
                        matched_other = True
                        break
                if not matched_other:
                    matched_words.append(word)
                    continue
            elif word_center > col_x1:
                spec_col_x0 = None
                if '规格型号' in col_x_ranges:
                    spec_col_x0, _ = col_x_ranges['规格型号']
                if spec_col_x0 is None or word_center < spec_col_x0:
                    matched_other = False
                    for other_keyword, (other_x0, other_x1) in col_x_ranges.items():
                        if other_keyword == keyword:
                            continue
                        if other_x0 <= word_center <= other_x1:
                            matched_other = True
                            break
                    if not matched_other:
                        matched_words.append(word)
                        continue

        if col_x0 <= word_center <= col_x1:
            matched_other_columns = []
            for other_keyword, (other_x0, other_x1) in col_x_ranges.items():
                if other_keyword == keyword:
                    continue
                if other_x0 <= word_center <= other_x1:
                    other_col_center = (other_x0 + other_x1) / 2
                    matched_other_columns.append((other_keyword, other_col_center))

            if matched_other_columns:
                current_distance = abs(word_center - col_center)
                other_distances = [(kw, abs(word_center - oc)) for kw, oc in matched_other_columns]
                min_other_distance = min(d for _, d in other_distances)
                if current_distance > min_other_distance:
                    continue

            matched_words.append(word)

    return matched_words


def _column_value_candidates(
    col_x_ranges: dict,
    sorted_line_words: List[dict],
    keyword: str,
) -> List[str]:
    """同列全部候选文本（按 x0 排序）。"""
    words = _match_words_in_column(col_x_ranges, sorted_line_words, keyword)
    words.sort(key=lambda w: w['x0'])
    values = []
    for word in words:
        text = clean_garbled_chars(word['text']).strip()
        if text:
            values.append(text)
    return values


def _reconcile_unit_price(
    item: LineItem,
    col_x_ranges: dict,
    sorted_numeric_words: List[dict],
    pdf_path: str,
    y: float,
) -> None:
    """叠印混排行按金额校正单价（数值列顺序可能与名称片段索引不一致）。"""
    if not item.amount or not item.price:
        return
    candidates = _column_value_candidates(col_x_ranges, sorted_numeric_words, '单价')
    price_candidates = [
        c for c in candidates
        if re.search(r'\d+\.\d+', c) and '%' not in c
    ]
    if len(price_candidates) <= 1:
        return
    if amount_matches_qty_price(item.quantity, item.price, item.amount):
        return
    picked = pick_price_matching_amount(
        price_candidates, item.quantity, item.amount,
    )
    if (
        picked
        and picked != item.price
        and amount_matches_qty_price(item.quantity, picked, item.amount)
    ):
        logger.debug(
            f'{pdf_path}: 行Y={y:.2f} 按金额校正单价: '
            f'"{item.price}" -> "{picked}"'
        )
        item.price = picked


def _create_column_matcher(
    col_x_ranges: dict,
    sorted_line_words: List[dict],
    value_index: Optional[int] = None,
) -> callable:
    """
    创建列匹配函数

    value_index: 叠印同行多商品垂直叠置时，取该列第 N 个匹配词（0-based）
    """
    def find_word_in_column(keyword: str) -> Optional[str]:
        """在指定列中查找单词，返回该列中所有匹配单词的合并文本"""
        if keyword not in col_x_ranges:
            return None

        matched_words = _match_words_in_column(col_x_ranges, sorted_line_words, keyword)
        
        if not matched_words:
            return None

        matched_words.sort(key=lambda w: w['x0'])
        if value_index is not None and len(matched_words) > 1:
            if value_index < len(matched_words):
                matched_words = [matched_words[value_index]]
            else:
                matched_words = [matched_words[-1]]

        result = ' '.join([clean_garbled_chars(w['text']).strip() for w in matched_words])
        return result.strip() if result.strip() else None
    
    return find_word_in_column


def _find_item_name(
    sorted_line_words: List[dict],
    col_x_ranges: dict,
    find_word_in_column: callable,
    pdf_path: str = '',
) -> str:
    """
    查找项目名称
    
    策略：
    1. 片段内仅一个 *类别* 词时直接使用（叠印叠置行）
    2. 优先在项目名称列中查找
    3. 如果列中没找到，查找最左侧包含中文的单词
    """
    item_name = ''

    name_hits = [
        w for w in sorted_line_words
        if has_project_name_at_start(clean_garbled_chars(w.get('text', '')))
    ]
    if len(name_hits) == 1:
        single = clean_garbled_chars(name_hits[0]['text']).strip()
        if '项目名称' in col_x_ranges:
            col_name = find_word_in_column('项目名称') or ''
            if col_name and len(col_name.replace(' ', '')) > len(single.replace(' ', '')):
                return col_name.strip()
        return single
    
    # 方法1：在项目名称列中查找
    if '项目名称' in col_x_ranges:
        item_name = find_word_in_column('项目名称') or ''
        # 如果找到的是项目名称模式，直接使用
        if item_name and PROJECT_NAME_REGEX.search(item_name):
            pass  # 已经是项目名称格式
        elif item_name and not re.search(r'[\u4e00-\u9fa5]', item_name):
            # 如果找到的不是中文，可能不是项目名称，清空
            item_name = ''
    
    # 方法2：如果列中没找到，查找最左侧包含中文的单词
    if not item_name:
        for word in sorted_line_words:
            candidate_text = clean_garbled_chars(word['text'])
            word_center = (word['x0'] + word['x1']) / 2
            
            # 检查该单词是否已经匹配到其他列（排除项目名称列）
            # 如果已经匹配到其他列，不应该作为项目名称
            matched_other_column = False
            for keyword, (col_x0, col_x1) in col_x_ranges.items():
                if keyword == '项目名称':
                    continue
                # 检查单词中心点是否在该列的范围内
                if col_x0 <= word_center <= col_x1:
                    matched_other_column = True
                    break
            
            # 如果已经匹配到其他列，跳过该单词
            if matched_other_column:
                continue
            
            # 首先尝试严格匹配（*项目名称*费用名称）
            if PROJECT_NAME_REGEX.search(candidate_text):
                item_name = candidate_text.strip()
                break
            # 如果严格匹配失败，尝试放宽条件：包含中文且不是纯数字
            elif re.search(r'[\u4e00-\u9fa5]', candidate_text) and not re.match(r'^[\d\s\.]+$', candidate_text):
                # 检查是否可能是项目名称（长度合理，包含中文）
                if len(candidate_text.strip()) >= 2 and len(candidate_text.strip()) <= 100:
                    item_name = candidate_text.strip()
                    logger.debug(f'{pdf_path}: 使用放宽条件匹配到项目名称: {item_name[:50]}')
                    break
    
    return item_name


def _make_orphan_suffix_item(suffix_text: str, y: float, pdf_path: str = '') -> Optional[LineItem]:
    """构造项目名称折行 suffix 明细项"""
    if not suffix_text or not is_orphan_suffix_fragment_text(suffix_text):
        return None
    item = LineItem()
    item.item_name = suffix_text
    item.source_y = y
    logger.debug(f'{pdf_path}: 行Y={y:.2f} 混排剥离续行: "{suffix_text}"')
    return item


def _last_incomplete_amount_item(items: List[LineItem]) -> Optional[LineItem]:
    """返回列表中最近一条名称未闭合且有金额的明细"""
    for it in reversed(items):
        if not it.amount or not it.amount.strip():
            continue
        if _item_name_needs_continuation(it.item_name):
            return it
    return None


def _should_merge_as_orphan_suffix(text: str) -> bool:
    """短 suffix 用 orphan 逻辑；长规格描述行整段拼接到项目名称"""
    compact = (text or '').replace(' ', '').strip()
    if not compact or len(compact) > 15:
        return False
    return is_orphan_suffix_fragment_text(compact)


def _merge_orphan_suffix_to_item(
    merged_item: LineItem,
    orphan_item: LineItem,
    merge_spec: bool = True,
) -> None:
    """合并续行 suffix（混排一行多 suffix 时只取与 parent 匹配的一段）"""
    picked = pick_orphan_suffix_for_parent(
        merged_item.item_name or '', orphan_item.item_name or '',
    )
    if not picked:
        return
    merged_item.item_name = merge_item_name_suffix(
        merged_item.item_name or '', picked,
    )


def _extract_item_from_row(
    y: float,
    line_words: List[dict],
    col_x_ranges: dict,
    pdf_path: str = '',
    numeric_source_words: Optional[List[dict]] = None,
    column_value_index: Optional[int] = None,
) -> Optional[LineItem]:
    """
    从单行数据中提取明细项
    
    Args:
        y: 行的Y坐标
        line_words: 行的单词列表
        col_x_ranges: 列的X坐标范围字典
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        LineItem 对象，如果该行不是有效明细行则返回None
    """
    if not line_words:
        return None

    name_words = strip_orphan_prefix_words(line_words)
    if not name_words:
        return None
    if is_orphan_suffix_only_row(name_words):
        suffix_text = extract_orphan_suffix_text(name_words)
        if not suffix_text or is_buyer_seller_fragment_text(suffix_text):
            return None
        if DATE_REGEX_LOOSE.search(suffix_text):
            return None
        frags = split_orphan_suffix_fragments(suffix_text, name_words)
        frag = frags[0] if frags else suffix_text
        item = LineItem()
        item.item_name = frag
        item.source_y = y
        logger.debug(
            f'{pdf_path}: 行Y={y:.2f} 项目名称折行续行: "{frag}"'
        )
        return item
    
    # 检查是否是合计行或其他非明细行
    row_text = ' '.join([w['text'] for w in sorted(name_words, key=lambda w: w['x0'])]).lower()
    if any(kw in row_text for kw in invoice_const.SUMMARY_ROW_KEYWORDS):
        return None
    
    # 按X坐标排序单词
    sorted_name_words = sorted(name_words, key=lambda w: w['x0'])
    sorted_numeric_words = sorted(
        (numeric_source_words if numeric_source_words is not None else name_words),
        key=lambda w: w['x0'],
    )
    
    # 创建列匹配器（叠印混排行用整行单词 + value_index 取第 N 列值）
    find_word_in_column = _create_column_matcher(
        col_x_ranges, sorted_numeric_words, value_index=column_value_index,
    )
    
    # 查找项目名称
    item_name = _find_item_name(sorted_name_words, col_x_ranges, find_word_in_column, pdf_path)
    
    # 创建明细项
    item = LineItem()
    item.item_name = clean_extracted_item_name(item_name)
    item.spec = find_word_in_column('规格型号') or ''
    item.unit = find_word_in_column('单位') or ''
    item.quantity = find_word_in_column('数量') or ''
    item.price = find_word_in_column('单价') or ''
    item.amount = find_word_in_column('金额') or ''
    item.tax_rate = find_word_in_column('税率') or ''
    item.tax_amount = find_word_in_column('税额') or ''

    if not item.item_name:
        has_spec_only = bool(item.spec and item.spec.strip())
        if not _line_item_has_numeric_data(item) and not has_spec_only:
            return None
    elif DATE_REGEX_LOOSE.search(item.item_name) and not PROJECT_NAME_REGEX.search(item.item_name):
        return None

    if not item.quantity.strip() and item.price and item.amount:
        qty, fixed_price = split_quantity_prefix_from_price(item.price, item.amount)
        if qty:
            item.quantity = qty
            item.price = fixed_price
            logger.debug(
                f'{pdf_path}: 行Y={y:.2f} 拆分粘连数量/单价: '
                f'数量="{qty}", 单价="{fixed_price}"'
            )

    _reconcile_unit_price(item, col_x_ranges, sorted_numeric_words, pdf_path, y)
    
    if _has_concatenated_column_values(item):
        logger.debug(f'{pdf_path}: 行Y={y:.2f} 列字段含多个数值（串行），丢弃该解析结果')
        return None

    # 如果这一行有任何字段，就保留（可能是明细行的一部分）
    if item.item_name or item.spec or item.unit or item.quantity or item.price or item.amount:
        # 打印明细数据行的x0坐标信息
        logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
        logger.debug(f'{pdf_path}: 明细数据行 (Y={y:.2f}) X坐标信息:')
        for word in sorted_name_words:
            word_text = clean_garbled_chars(word['text'])
            # 判断该单词属于哪一列（与find_word_in_column逻辑保持一致）
            matched_columns = []
            word_center = (word['x0'] + word['x1']) / 2
            
            for keyword, (col_x0, col_x1) in col_x_ranges.items():
                # 特殊处理：对于项目名称列，如果单词中心坐标小于项目名称列的x0，
                # 且没有匹配到其他列，则将其匹配到项目名称列
                is_item_name_column = (keyword == '项目名称')
                if is_item_name_column and word_center < col_x0:
                    # 检查该单词是否匹配到其他列
                    matched_other = False
                    for other_keyword, (other_x0, other_x1) in col_x_ranges.items():
                        if other_keyword == keyword:
                            continue
                        if other_x0 <= word_center <= other_x1:
                            matched_other = True
                            break
                    # 如果没有匹配到其他列，则匹配到项目名称列
                    if not matched_other:
                        matched_columns.append(keyword)
                        continue
                
                # 常规匹配：中心点在列范围内
                if col_x0 <= word_center <= col_x1:
                    matched_columns.append(keyword)
            
            column_info = f' -> [{", ".join(matched_columns)}]' if matched_columns else ' -> [未匹配到列]'
            logger.debug(f'{pdf_path}:   单词: "{word_text:20s}" | x0={word["x0"]:8.2f} | x1={word["x1"]:8.2f} | 中心={((word["x0"]+word["x1"])/2):8.2f}{column_info}')
        
        logger.debug(f'{pdf_path}: 解析结果: 项目名称="{item.item_name}", 金额="{item.amount}", 规格="{item.spec}", 单位="{item.unit}", 数量="{item.quantity}", 单价="{item.price}", 税率="{item.tax_rate}", 税额="{item.tax_amount}"')
        logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
        item.source_y = y
        return item
    
    return None


def _extract_items_from_table_row(
    y: float,
    line_words: List[dict],
    col_x_ranges: dict,
    pdf_path: str,
    layout_for_parse,
    dedup_tracker: OverlayItemDedupTracker,
    lines_dict: Optional[dict] = None,
    peel_for_previous: bool = False,
) -> List[LineItem]:
    """从表格行提取明细（支持叠印合并行拆分与去重）"""
    line_words = filter_row_noise_words(line_words, col_x_ranges)
    if not line_words:
        return []

    items: List[LineItem] = []

    if peel_for_previous:
        prev_suffix, working_words = extract_orphan_suffix_for_previous_row(
            line_words, col_x_ranges, peel_for_previous=True,
        )
        if prev_suffix:
            orphan = _make_orphan_suffix_item(prev_suffix, y, pdf_path)
            if orphan:
                items.append(orphan)
    else:
        working_words = line_words

    name_words_check = strip_orphan_prefix_words(working_words)
    if is_orphan_suffix_only_row(name_words_check):
        suffix_text = extract_orphan_suffix_text(name_words_check)
        for frag in split_orphan_suffix_fragments(suffix_text, name_words_check):
            orphan = _make_orphan_suffix_item(frag, y, pdf_path)
            if orphan:
                items.append(orphan)
        return items

    segments = split_merged_detail_row(working_words)
    multi_segment = len(segments) > 1
    for si, segment_words in enumerate(segments):
        if lines_dict and layout_for_parse:
            if is_segment_overlay_backward_copy(
                y, segment_words, lines_dict, layout_for_parse, col_x_ranges,
            ):
                continue
            if is_segment_overlay_forward_inferior_copy(
                y, segment_words, lines_dict, layout_for_parse, col_x_ranges,
            ):
                continue
        item = _extract_item_from_row(
            y,
            segment_words,
            col_x_ranges,
            pdf_path,
            numeric_source_words=working_words if multi_segment else None,
            column_value_index=si if multi_segment else None,
        )
        if not item:
            continue
        if dedup_tracker.should_keep(item.item_name, item.amount, y, layout_for_parse):
            items.append(item)
    return items


def parse_line_items_from_words_raw(
    table_words: List[dict],
    pdf_path: str = '',
    page_num: int = 1,
    layout_from_partition: Optional[LayoutBounds] = None,
) -> List[LineItem]:
    """
    从表格区域的单词列表中解析明细行（返回原始明细项，不进行跨行合并）
    
    用于多页发票处理：分别提取每页的 raw_items，然后统一进行跨行合并
    
    Args:
        table_words: 表格区域的单词列表
        pdf_path: PDF 文件路径（用于日志）
        page_num: 页码（用于日志）
        
    Returns:
        原始商品明细列表（未合并）
    """
    if not table_words or len(table_words) == 0:
        logger.debug(f'{pdf_path} 第{page_num}页: 表格数据为空，无法解析明细')
        return []

    # 将单词按行分组
    lines_dict = group_words_by_y(table_words, y_tolerance=3.0)
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0])

    logger.debug(f'{pdf_path} 第{page_num}页: 表格共有 {len(sorted_lines)} 行')

    layout_for_parse = resolve_layout_for_table_parse(lines_dict, layout_from_partition)

    # 找到表头行（叠印时优先主层表头）
    header_row_y = find_primary_header_row_y(sorted_lines, layout_for_parse)
    if header_row_y is None:
        header_row_y = _find_header_row(sorted_lines, pdf_path)
    if header_row_y is None:
        logger.debug(f'{pdf_path} 第{page_num}页: 未找到表头行')
        return []

    # 识别各列的X坐标范围（通过表头行的单词位置）
    header_line_words = sorted(lines_dict[header_row_y], key=lambda w: w['x0'])
    keyword_words = _match_keywords_to_words(header_line_words, pdf_path)
    col_x_ranges, _ = _calculate_column_ranges(keyword_words, pdf_path)

    # 遍历数据行（表头行之后的行），提取字段（不进行跨行合并）
    raw_items = []
    header_found = False
    dedup_tracker = OverlayItemDedupTracker(
        overlay_y_offset=layout_for_parse.overlay_y_offset if layout_for_parse else None,
    )

    for y, line_words in sorted_lines:
        if y == header_row_y:
            header_found = True
            continue
        if not header_found:
            continue

        if should_skip_table_row(
            y, line_words, header_row_y, layout_for_parse, col_x_ranges, lines_dict,
        ):
            continue

        peel_for_previous = _last_incomplete_amount_item(raw_items) is not None
        raw_items.extend(
            _extract_items_from_table_row(
                y, line_words, col_x_ranges, pdf_path, layout_for_parse, dedup_tracker,
                lines_dict,
                peel_for_previous=peel_for_previous,
            )
        )

    logger.debug(f'{pdf_path} 第{page_num}页: 提取到 {len(raw_items)} 条原始明细项（未合并）')

    return raw_items


def _extract_column_starts_from_table_words(table_words: List[dict], pdf_path: str = '') -> dict:
    """
    从表格区域的单词列表中提取列坐标信息（用于跨行合并）
    
    通过查找表头行来识别各列的X坐标
    
    Args:
        table_words: 表格区域的单词列表（可能包含多页数据）
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        各列的首字x0坐标字典 {keyword: x0}
    """
    if not table_words or len(table_words) == 0:
        return {}
    
    # 将单词按行分组
    lines_dict = group_words_by_y(table_words, y_tolerance=3.0)
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0])

    layout_for_parse = resolve_layout_for_table_parse(lines_dict, None)

    # 找到表头行（通常是第一行或包含表头关键词的行）
    header_row_y = find_primary_header_row_y(sorted_lines, layout_for_parse)
    if header_row_y is None:
        header_row_y = _find_header_row(sorted_lines, pdf_path)
    if header_row_y is None:
        logger.warning(f'{pdf_path}: 未能找到表头行，无法提取列坐标信息')
        return {}
    
    # 识别各列的X坐标范围（通过表头行的单词位置）
    header_line_words = sorted(lines_dict[header_row_y], key=lambda w: w['x0'])
    keyword_words = _match_keywords_to_words(header_line_words, pdf_path)
    _, col_x_starts = _calculate_column_ranges(keyword_words, pdf_path)
    
    return col_x_starts


def parse_line_items_from_words(table_words: List[dict], pdf_path: str = '') -> List[LineItem]:
    """
    从表格区域的单词列表中解析明细行
    
    Args:
        table_words: 表格区域的单词列表
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        商品明细列表
    """
    if not table_words or len(table_words) == 0:
        logger.warning(f'{pdf_path}: 表格数据为空，无法解析明细')
        return []

    # 将单词按行分组
    lines_dict = group_words_by_y(table_words, y_tolerance=3.0)
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0])

    logger.debug(f'{pdf_path}: 表格共有 {len(sorted_lines)} 行')

    layout_for_parse = resolve_layout_for_table_parse(lines_dict, None)

    # 找到表头行
    header_row_y = find_primary_header_row_y(sorted_lines, layout_for_parse)
    if header_row_y is None:
        header_row_y = _find_header_row(sorted_lines, pdf_path)
    if header_row_y is None:
        return []

    # 识别各列的X坐标范围（通过表头行的单词位置）
    header_line_words = sorted(lines_dict[header_row_y], key=lambda w: w['x0'])
    keyword_words = _match_keywords_to_words(header_line_words, pdf_path)
    col_x_ranges, col_x_starts = _calculate_column_ranges(keyword_words, pdf_path)

    # 遍历数据行（表头行之后的行），提取字段
    raw_items = []
    header_found = False
    dedup_tracker = OverlayItemDedupTracker(
        overlay_y_offset=layout_for_parse.overlay_y_offset if layout_for_parse else None,
    )

    for y, line_words in sorted_lines:
        if y == header_row_y:
            header_found = True
            continue
        if not header_found:
            continue

        if should_skip_table_row(
            y, line_words, header_row_y, layout_for_parse, col_x_ranges, lines_dict,
        ):
            continue

        peel_for_previous = _last_incomplete_amount_item(raw_items) is not None
        raw_items.extend(
            _extract_items_from_table_row(
                y, line_words, col_x_ranges, pdf_path, layout_for_parse, dedup_tracker,
                lines_dict,
                peel_for_previous=peel_for_previous,
            )
        )

    items = merge_split_line_items(
        raw_items, col_x_starts, pdf_path,
        layout=layout_for_parse,
        lines_dict=lines_dict,
    )

    logging.info(f'{pdf_path}: 合并后共有 {len(items)} 条明细')

    if len(items) == 0 and len(raw_items) > 0:
        logger.warning(f'{pdf_path}: 处理了 {len(raw_items)} 行数据，但未能解析到任何商品明细')

    return items


def _is_item_name_prefix_first_line(name: str) -> bool:
    """是否为 *类别*商品名 首行前缀（后续常有规格折行）"""
    if not name or not name.strip():
        return False
    compact = name.strip().replace(' ', '')
    return bool(re.match(r'^\*[^*]+\*[^*]+', compact))


def _parent_allows_name_continuation(
    parent_item_name: Optional[str],
    layout: Optional[LayoutBounds] = None,
) -> bool:
    """
    上一行项目名称是否仍可能接折行。

    - 叠印：仅括号未闭合时接续行（orphan suffix 易串到相邻商品，禁止 *类别* 首行泛化续接）
    - 非叠印：另允许 *类别* 首行后接多行规格描述（如绿林工具发票）
    """
    if not parent_item_name or not parent_item_name.strip():
        return False
    if _item_name_needs_continuation(parent_item_name):
        return True
    if layout and layout.has_overlay:
        return False
    return _is_item_name_prefix_first_line(parent_item_name)


def _item_name_needs_continuation(name: str) -> bool:
    """项目名称是否未结束（括号未闭合等），需要折行续接"""
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


def _is_plausible_name_continuation_text(
    text: str,
    parent_item_name: Optional[str] = None,
    layout: Optional[LayoutBounds] = None,
) -> bool:
    """判断文本是否像商品名折行续接（排除购销方噪声）"""
    compact = text.strip().replace(' ', '')
    if not compact:
        return False
    if is_buyer_seller_fragment_text(compact):
        return False
    if DATE_REGEX_LOOSE.search(compact):
        return False
    if compact.startswith('*') and PROJECT_NAME_REGEX.search(compact):
        return False
    if re.search(r'[）)]', compact):
        if parent_item_name and _item_name_needs_continuation(parent_item_name):
            return suffix_completes_parent_name(parent_item_name, compact)
        if layout and layout.has_overlay:
            return False
        return True
    if layout and layout.has_overlay:
        return False
    if parent_item_name and _is_item_name_prefix_first_line(parent_item_name):
        if not _is_item_name_prefix_first_line(compact):
            if re.search(r'[\u4e00-\u9fa5A-Za-z0-9]', compact):
                return True
    if parent_item_name and _item_name_needs_continuation(parent_item_name):
        return len(compact) <= 8 and bool(re.search(r'[\u4e00-\u9fa5]', compact))
    return False


def _should_discard_unmerged_item(item: LineItem) -> bool:
    """丢弃未能合并的噪声行（续行碎片、日期、购销方字等）"""
    if item.amount and item.amount.strip():
        return False
    name = (item.item_name or '').strip().replace(' ', '')
    if not name:
        return True
    if DATE_REGEX_LOOSE.search(name):
        return True
    if is_buyer_seller_fragment_text(name):
        return True
    if _is_item_name_first_line(item):
        return False
    if not name.startswith('*'):
        return True
    return True


def _has_concatenated_column_values(item: LineItem) -> bool:
    """列字段是否串了多个数值（叠印同行多商品未拆开）"""
    for value in (item.amount, item.price, item.quantity, item.tax_amount, item.unit, item.tax_rate):
        if not value or ' ' not in value.strip():
            continue
        tokens = value.strip().split()
        numeric = [t for t in tokens if re.match(r'^[\d.]+%?$', t)]
        if len(numeric) >= 2:
            return True
    return False


def _is_item_name_first_line(item: LineItem) -> bool:
    """
    判断是否是项目名称的首行
    
    首行特征：匹配 *商品名称* 格式（如 *金属制品*绿林）
    要求：* 必须在字符串开头（允许前导空格），确保不会误匹配中间包含 * 的文本
    
    Args:
        item: 商品明细项
        
    Returns:
        如果是项目名称首行返回True
    """
    if not item.item_name or not item.item_name.strip():
        return False
    
    item_name = item.item_name.strip()
    
    return has_project_name_at_start(item_name)


def _is_item_name_continuation_line(
    item: LineItem,
    parent_item_name: Optional[str] = None,
    parent_source_y: Optional[float] = None,
    layout: Optional[LayoutBounds] = None,
    lines_dict: Optional[dict] = None,
    predecessor_source_y: Optional[float] = None,
) -> bool:
    """
    判断是否是项目名称的后续行（跨行）
    
    判断条件：
    1. 有项目名称字段
    2. 没有金额字段（有金额说明是完整商品）
    3. 不匹配首行格式（*商品名称*，且 * 必须在开头）
    4. 字段数量较少（主要是项目名称内容）
    5. 上一行项目名称尚未结束（括号未闭合等）
    
    Args:
        item: 商品明细项
        parent_item_name: 上一行已合并的项目名称
        
    Returns:
        如果是项目名称的后续行返回True
    """
    if not item.item_name or not item.item_name.strip():
        return False

    if parent_item_name is not None and not _parent_allows_name_continuation(
        parent_item_name, layout,
    ):
        return False

    if layout and layout.has_overlay and lines_dict and item.source_y is not None:
        if is_overlay_orphan_suffix_text_duplicate(
            item.source_y, item.item_name or '', lines_dict, layout,
        ):
            logger.debug(
                '跨行合并: 跳过叠印续行副本 y=%.2f 文本="%s"',
                item.source_y, (item.item_name or '')[:20],
            )
            return False

    cont = item.item_name.strip()
    picked = pick_orphan_suffix_for_parent(
        parent_item_name or '', cont,
    ) if parent_item_name else cont

    if layout and layout.has_overlay and lines_dict and item.source_y is not None:
        pred_y = predecessor_source_y if predecessor_source_y is not None else parent_source_y
        y_ok = False
        ref_ys = []
        if pred_y is not None:
            ref_ys.append(pred_y)
        if parent_source_y is not None and parent_source_y not in ref_ys:
            ref_ys.append(parent_source_y)
        for ref_y in ref_ys:
            if orphan_suffix_owned_by_parent_y(
                item.source_y, ref_y, picked, layout, lines_dict,
            ):
                y_ok = True
                break
        if not y_ok:
            logger.debug(
                '跨行合并: 续行 y=%.2f 不属于主行 y=%.2f 文本="%s"',
                item.source_y, pred_y or parent_source_y or 0, picked[:20],
            )
            return False
    elif predecessor_source_y is not None and item.source_y is not None:
        if not continuation_y_matches_predecessor(
            predecessor_source_y, item.source_y, layout, allow_cross_block=True,
        ):
            if not _parent_allows_name_continuation(parent_item_name or '', layout):
                return False

    if not _is_plausible_name_continuation_text(picked, parent_item_name, layout):
        return False

    if cont.startswith('*') and PROJECT_NAME_REGEX.search(cont):
        return False
    
    # 有金额说明是完整商品，不是跨行
    if item.amount and item.amount.strip():
        return False
    
    # 不匹配首行格式（使用与 _is_item_name_first_line 相同的判断逻辑）
    # 确保 * 在开头，而不是在中间
    is_not_first_line = not _is_item_name_first_line(item)
    
    # 统计非空字段数量
    field_count = sum([
        1 if item.item_name and item.item_name.strip() else 0,
        1 if item.spec and item.spec.strip() else 0,
        1 if item.unit and item.unit.strip() else 0,
        1 if item.quantity and item.quantity.strip() else 0,
        1 if item.price and item.price.strip() else 0,
        1 if item.tax_rate and item.tax_rate.strip() else 0,
        1 if item.tax_amount and item.tax_amount.strip() else 0,
    ])
    
    # 主要是项目名称内容（字段数量较少）
    return is_not_first_line and field_count <= 2


def _is_spec_continuation_line(item: LineItem) -> bool:
    """
    判断是否是规格型号的折行
    
    判断条件：
    1. 有规格字段
    2. 没有金额字段
    3. 字段数量较少（主要是规格内容）
    
    Args:
        item: 商品明细项
        
    Returns:
        如果是规格型号的折行返回True
    """
    if not item.spec or not item.spec.strip():
        return False
    
    # 有金额说明是完整商品，不是折行
    if item.amount and item.amount.strip():
        return False
    
    # 统计非空字段数量
    field_count = sum([
        1 if item.item_name and item.item_name.strip() else 0,
        1 if item.spec and item.spec.strip() else 0,
        1 if item.unit and item.unit.strip() else 0,
        1 if item.quantity and item.quantity.strip() else 0,
        1 if item.price and item.price.strip() else 0,
        1 if item.tax_rate and item.tax_rate.strip() else 0,
        1 if item.tax_amount and item.tax_amount.strip() else 0,
    ])
    
    # 主要是规格内容（字段数量较少）
    return field_count <= 2


def _is_single_field_line(item: LineItem) -> bool:
    """
    判断是否是只有单个字段的行（如只有单价、只有数量等）
    
    这种情况可能是字段被拆分到不同行了，需要合并后继续检查后续是否有项目名称跨行
    
    Args:
        item: 商品明细项
        
    Returns:
        如果是单个字段行返回True
    """
    # 有金额说明是完整商品，不是单个字段行
    if item.amount and item.amount.strip():
        return False
    
    # 有项目名称首行格式，不是单个字段行
    if _is_item_name_first_line(item):
        return False
    
    # 统计非空字段数量
    field_count = sum([
        1 if item.item_name and item.item_name.strip() else 0,
        1 if item.spec and item.spec.strip() else 0,
        1 if item.unit and item.unit.strip() else 0,
        1 if item.quantity and item.quantity.strip() else 0,
        1 if item.price and item.price.strip() else 0,
        1 if item.tax_rate and item.tax_rate.strip() else 0,
        1 if item.tax_amount and item.tax_amount.strip() else 0,
    ])
    
    # 只有1个字段，且不是新的项目名称首行
    return field_count == 1 and not _is_item_name_first_line(item)


def _is_generic_continuation_line(
    item: LineItem,
    parent_item_name: Optional[str] = None,
    parent_source_y: Optional[float] = None,
    layout: Optional[LayoutBounds] = None,
    lines_dict: Optional[dict] = None,
) -> bool:
    """
    通用的折行判断（保留原有逻辑）
    
    原有逻辑：如果一行只有1-2个字段且没有金额，被认为是折行字段
    这个逻辑是通用的，不区分是项目名称还是规格，用于兜底处理
    
    Args:
        item: 商品明细项
        parent_item_name: 上一行已合并的项目名称
        
    Returns:
        如果是通用折行返回True
    """
    # 有金额说明是完整商品，不是折行
    if item.amount and item.amount.strip():
        return False
    
    # 有项目名称首行格式，不是通用折行（已经单独处理）
    if _is_item_name_first_line(item):
        return False

    if parent_item_name is not None and not _parent_allows_name_continuation(
        parent_item_name, layout,
    ):
        return False

    if item.item_name and not _is_plausible_name_continuation_text(
        item.item_name.strip(), parent_item_name, layout,
    ):
        return False

    if layout and layout.has_overlay and lines_dict and item.source_y is not None:
        if is_overlay_orphan_suffix_text_duplicate(
            item.source_y, item.item_name or '', lines_dict, layout,
        ):
            return False
        ref_y = parent_source_y
        if ref_y is not None and not orphan_suffix_owned_by_parent_y(
            item.source_y,
            ref_y,
            item.item_name or '',
            layout,
            lines_dict,
        ):
            return False

    if item.item_name:
        cont = item.item_name.strip().replace(' ', '')
        if _is_item_name_prefix_first_line(cont):
            return False
    
    # 统计非空字段数量
    field_count = sum([
        1 if item.item_name and item.item_name.strip() else 0,
        1 if item.spec and item.spec.strip() else 0,
        1 if item.unit and item.unit.strip() else 0,
        1 if item.quantity and item.quantity.strip() else 0,
        1 if item.price and item.price.strip() else 0,
        1 if item.tax_rate and item.tax_rate.strip() else 0,
        1 if item.tax_amount and item.tax_amount.strip() else 0,
    ])
    
    # 只有1-2个字段，可能是折行（通用判断，保留原有逻辑）
    return field_count <= 2


def _merge_fields_from_item(merged_item: LineItem, next_item: LineItem, merge_spec: bool = True) -> None:
    """
    将next_item的字段合并到merged_item中
    
    Args:
        merged_item: 目标合并项
        next_item: 源项
        merge_spec: 是否合并规格型号（True表示拼接，False表示互补）
    """
    # 项目名称：拼接
    if next_item.item_name and next_item.item_name.strip():
        if merged_item.item_name:
            merged_item.item_name = f'{merged_item.item_name}{next_item.item_name}'.strip().replace(' ', '')
        else:
            merged_item.item_name = next_item.item_name.strip().replace(' ', '')
    
    # 规格型号：根据merge_spec决定是拼接还是互补
    if next_item.spec and next_item.spec.strip():
        if merge_spec:
            # 拼接模式（用于折行）
            if merged_item.spec:
                merged_item.spec = f'{merged_item.spec}{next_item.spec}'.strip().replace(' ', '')
            else:
                merged_item.spec = next_item.spec.strip().replace(' ', '')
        else:
            # 互补模式（仅在merged_item没有时才设置）
            if not merged_item.spec:
                merged_item.spec = next_item.spec.strip().replace(' ', '')
    
    # 其他字段：互补模式（仅在merged_item没有时才设置）
    if next_item.unit and next_item.unit.strip() and not merged_item.unit:
        merged_item.unit = next_item.unit.strip().replace(' ', '')
    if next_item.quantity and next_item.quantity.strip() and not merged_item.quantity:
        merged_item.quantity = next_item.quantity.strip().replace(' ', '')
    if next_item.price and next_item.price.strip() and not merged_item.price:
        merged_item.price = next_item.price.strip().replace(' ', '')
    if next_item.amount and next_item.amount.strip() and not merged_item.amount:
        merged_item.amount = next_item.amount.strip().replace(' ', '')
    if next_item.tax_rate and next_item.tax_rate.strip() and not merged_item.tax_rate:
        merged_item.tax_rate = next_item.tax_rate.strip().replace(' ', '')
    if next_item.tax_amount and next_item.tax_amount.strip() and not merged_item.tax_amount:
        merged_item.tax_amount = next_item.tax_amount.strip().replace(' ', '')




def _merge_continuation_lines(
    merged_item: LineItem,
    start_index: int,
    items: List[LineItem],
    pdf_path: str = '',
    layout: Optional[LayoutBounds] = None,
    lines_dict: Optional[dict] = None,
) -> int:
    """
    统一的合并循环逻辑：从start_index开始向后扫描，合并跨行和折行的记录
    
    使用统一的检查顺序（情况1的逻辑）：
    1. 遇到新的项目名称首行 → 停止
    2. 遇到有金额的行：
       - 如果merged_item无金额 → 合并，继续
       - 如果merged_item已有金额 → 停止
    3. 项目名称跨行 → 合并
    4. 规格型号折行 → 合并
    5. 单个字段行 → 合并
    6. 通用折行 → 合并
    7. 互补字段合并 → 合并
    8. 其他情况 → 停止
    
    Args:
        merged_item: 当前正在合并的商品项
        start_index: 开始扫描的位置（从start_index+1开始）
        items: 所有商品明细列表
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        合并结束的位置（下一个要处理的索引）
    """
    j = start_index + 1
    last_merged_y = merged_item.source_y
    while j < len(items):
        next_item = items[j]
        
        # 1. 遇到项目名称首行
        if _is_item_name_first_line(next_item):
            # 特殊情况：当前行有金额/数量等但无项目名称，下一行是仅带名称（及税额）的首行 → 互补合并（名称行补到上一行）
            merged_has_value = (
                (merged_item.amount and merged_item.amount.strip()) or
                (merged_item.quantity and merged_item.quantity.strip()) or
                (merged_item.price and merged_item.price.strip())
            )
            merged_no_name = not (merged_item.item_name and merged_item.item_name.strip())
            next_no_amount = not (next_item.amount and next_item.amount.strip())
            if merged_has_value and merged_no_name and next_no_amount:
                _merge_fields_from_item(merged_item, next_item, merge_spec=False)
                logger.debug(f'{pdf_path}: 合并名称行到上一行(有金额无名称): 行{start_index} + 行{j}, 项目名称="{merged_item.item_name[:50] if merged_item.item_name else ""}"')
                j += 1
                continue
            logger.debug(f'{pdf_path}: 行{j}是新的项目名称首行，停止当前商品合并')
            break
        
        # 2. 如果下一行有金额，但不是新的项目名称首行
        if next_item.amount and next_item.amount.strip():
            # 如果当前合并项还没有金额，合并这一行
            if not merged_item.amount or not merged_item.amount.strip():
                # 合并金额和其他字段（金额行需要合并规格型号）
                if next_item.amount and next_item.amount.strip():
                    merged_item.amount = next_item.amount.strip().replace(' ', '')
                _merge_fields_from_item(merged_item, next_item, merge_spec=True)
                logger.debug(f'{pdf_path}: 合并金额行: 行{start_index} + 行{j}, 金额="{merged_item.amount}", 规格="{merged_item.spec[:50] if merged_item.spec else ""}"')
                j += 1
                continue
            else:
                # 当前已有金额，遇到新的有金额行，停止合并
                logger.debug(f'{pdf_path}: 行{j}有金额且当前已有金额，停止当前商品合并')
                break
        
        # 3. 检查是否是项目名称的跨行
        if _is_item_name_continuation_line(
            next_item,
            merged_item.item_name,
            merged_item.source_y,
            layout,
            lines_dict,
            predecessor_source_y=last_merged_y,
        ):
            if _should_merge_as_orphan_suffix(next_item.item_name or ''):
                _merge_orphan_suffix_to_item(merged_item, next_item, merge_spec=True)
            else:
                _merge_fields_from_item(merged_item, next_item, merge_spec=True)
            logger.debug(f'{pdf_path}: 合并项目名称跨行: 行{start_index} + 行{j}, 项目名称="{merged_item.item_name[:50] if merged_item.item_name else ""}", 规格="{merged_item.spec[:50] if merged_item.spec else ""}"')
            last_merged_y = next_item.source_y
            j += 1
            continue
        
        # 4. 检查是否是规格型号的折行
        if _is_spec_continuation_line(next_item):
            _merge_fields_from_item(merged_item, next_item, merge_spec=True)
            logger.debug(f'{pdf_path}: 合并规格型号折行: 行{start_index} + 行{j}, 规格="{merged_item.spec[:50] if merged_item.spec else ""}"')
            last_merged_y = next_item.source_y
            j += 1
            continue
        
        # 5. 检查是否是单个字段行（如只有单价、只有数量等）
        if _is_single_field_line(next_item):
            if not _parent_allows_name_continuation(merged_item.item_name, layout):
                logger.debug(
                    f'{pdf_path}: 行{j} 单字段行但主行名称已闭合，停止当前商品合并'
                )
                break
            if next_item.item_name and not _is_plausible_name_continuation_text(
                next_item.item_name.strip(), merged_item.item_name, layout,
            ):
                logger.debug(
                    f'{pdf_path}: 行{j} 单字段行文本不像续行，停止当前商品合并'
                )
                break
            if layout and layout.has_overlay and lines_dict and next_item.source_y is not None:
                picked = pick_orphan_suffix_for_parent(
                    merged_item.item_name or '', next_item.item_name or '',
                )
                if not picked:
                    logger.debug(
                        f'{pdf_path}: 行{j} orphan suffix 与主行不匹配，停止当前商品合并'
                    )
                    break
                y_ok = False
                for ref_y in (last_merged_y, merged_item.source_y):
                    if ref_y is None:
                        continue
                    if orphan_suffix_owned_by_parent_y(
                        next_item.source_y, ref_y, picked, layout, lines_dict,
                    ):
                        y_ok = True
                        break
                if not y_ok:
                    logger.debug(
                        f'{pdf_path}: 行{j} orphan suffix Y 不匹配主行，停止当前商品合并'
                    )
                    break
            _merge_fields_from_item(merged_item, next_item, merge_spec=False)
            logger.debug(f'{pdf_path}: 合并单个字段行: 行{start_index} + 行{j}, 继续检查后续是否有项目名称跨行')
            last_merged_y = next_item.source_y
            j += 1
            continue
        
        # 6. 通用折行判断（保留原有逻辑，作为兜底）
        if _is_generic_continuation_line(
            next_item,
            merged_item.item_name,
            merged_item.source_y,
            layout,
            lines_dict,
        ):
            _merge_fields_from_item(merged_item, next_item, merge_spec=True)
            logger.debug(f'{pdf_path}: 合并通用折行字段: 行{start_index} + 行{j}, 项目名称="{merged_item.item_name[:50] if merged_item.item_name else ""}", 规格="{merged_item.spec[:50] if merged_item.spec else ""}"')
            j += 1
            continue
        
        # 7. 其他字段按互补逻辑合并
        if can_merge_items(merged_item, next_item):
            # 合并互补字段（规格型号需要拼接）
            if next_item.spec and next_item.spec.strip():
                if merged_item.spec:
                    merged_item.spec = f'{merged_item.spec}{next_item.spec}'.strip().replace(' ', '')
                else:
                    merged_item.spec = next_item.spec.strip().replace(' ', '')
            _merge_fields_from_item(merged_item, next_item, merge_spec=False)
            logger.debug(f'{pdf_path}: 合并互补字段: 行{start_index} + 行{j}, 规格="{merged_item.spec[:50] if merged_item.spec else ""}"')
            j += 1
            continue
        
        # 8. 不是需要合并的行；若主行仍缺续行则跳过该 orphan 继续向后找
        if (
            _item_name_needs_continuation(merged_item.item_name)
            and next_item.item_name
            and not (next_item.amount and next_item.amount.strip())
            and not _is_item_name_first_line(next_item)
        ):
            logger.debug(
                f'{pdf_path}: 行{j} 续行不匹配主行 y={merged_item.source_y}, 跳过继续查找'
            )
            j += 1
            continue
        break
    
    return j


def _is_orphan_suffix_candidate(item: LineItem) -> bool:
    """是否为未合并的项目名称折行续行候选"""
    if item.amount and item.amount.strip():
        return False
    if not item.item_name or not item.item_name.strip():
        return False
    if _is_item_name_first_line(item):
        return False
    return True


def _attach_orphan_suffixes_by_y(
    merged_items: List[LineItem],
    raw_items: List[LineItem],
    layout: Optional[LayoutBounds],
    lines_dict: Optional[dict],
    pdf_path: str = '',
) -> None:
    """
    按 Y 坐标将折行 suffix 挂接到名称未闭合的主层商品。

    叠印 PDF 中主层续行位置常被 B 套商品占用，suffix 仅出现在 overlay_y + line_gap 处。
    """
    if not lines_dict:
        return

    orphans = sorted(
        [
            it for it in raw_items
            if _is_orphan_suffix_candidate(it) and it.source_y is not None
        ],
        key=lambda it: it.source_y or 0,
    )
    if not orphans:
        return

    used_ids: set = set()
    frag_index_by_y: dict = {}

    for item in merged_items:
        if not item.item_name or not _item_name_needs_continuation(item.item_name):
            continue
        if item.source_y is None:
            continue

        best: Optional[LineItem] = None
        best_suffix = ''
        for orphan in orphans:
            if id(orphan) in used_ids:
                continue
            frags = split_orphan_suffix_fragments(orphan.item_name or '')
            idx = frag_index_by_y.get(orphan.source_y, 0)
            suffix = ''
            if idx < len(frags):
                candidate = frags[idx]
                if suffix_completes_parent_name(item.item_name, candidate):
                    suffix = candidate
            if not suffix:
                suffix = pick_orphan_suffix_for_parent(
                    item.item_name, orphan.item_name or '',
                )
            if not suffix or not suffix_completes_parent_name(item.item_name, suffix):
                continue
            if not _is_plausible_name_continuation_text(
                suffix, item.item_name, layout,
            ):
                continue
            if not orphan_suffix_owned_by_parent_y(
                orphan.source_y,
                item.source_y,
                suffix,
                layout,
                lines_dict,
            ):
                continue
            combined = merge_item_name_suffix(item.item_name, suffix)
            if _item_name_needs_continuation(combined):
                continue
            best = orphan
            best_suffix = suffix
            break

        if best is None or not best_suffix:
            continue

        frags = split_orphan_suffix_fragments(best.item_name or '')
        idx = frag_index_by_y.get(best.source_y, 0)
        if idx < len(frags) - 1:
            frag_index_by_y[best.source_y] = idx + 1
        else:
            used_ids.add(id(best))
            frag_index_by_y[best.source_y] = len(frags)

        item.item_name = merge_item_name_suffix(item.item_name, best_suffix)
        logger.debug(
            '%s: Y 坐标挂接续行 y=%.2f -> 主行 y=%.2f, 项目名称="%s"',
            pdf_path,
            best.source_y,
            item.source_y,
            item.item_name[:50],
        )


def merge_split_line_items(
    items: List[LineItem],
    col_x_starts: dict,
    pdf_path: str = '',
    layout: Optional[LayoutBounds] = None,
    lines_dict: Optional[dict] = None,
) -> List[LineItem]:
    """
    合并跨行的商品记录
    
    策略：
    1. 根据金额字段判断实际商品数量（有金额的记录才算一个商品）
    2. 识别项目名称首行（*商品名称* 格式），动态合并跨行的项目名称（不限制行数）
    3. 合并折行的规格型号（参考现有逻辑）
    4. 根据字段互补关系合并其他字段
    
    统一处理逻辑：
    - 情况1：项目名称首行 → 创建新对象并复制字段，使用统一合并循环
    - 情况2：有金额 → 直接使用current_item，使用统一合并循环
    - 情况3：无金额 → 使用current_item作为初始值，使用统一合并循环（查找和合并合并为一个循环）
    
    Args:
        items: 初始解析的商品明细列表（只包含商品明细数据）
        col_x_starts: 各列的首字x0坐标字典
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        合并后的商品明细列表
    """
    if not items or len(items) <= 1:
        return items
    
    # 统计有金额的记录数（实际商品数量）
    items_with_amount = [item for item in items if item.amount and item.amount.strip()]
    actual_item_count = len(items_with_amount)

    logger.debug(f'{pdf_path}: 初始解析 {len(items)} 条记录，其中 {actual_item_count} 条有金额字段')

    needs_name_merge = any(
        _item_name_needs_continuation(item.item_name)
        for item in items
        if item.item_name
    )
    has_continuation_rows = any(
        item.item_name and item.item_name.strip()
        and not (item.amount and item.amount.strip())
        and not _is_item_name_first_line(item)
        for item in items
    )
    if actual_item_count == len(items) and not needs_name_merge and not has_continuation_rows:
        return items
    
    # 合并逻辑：遍历items，合并跨行的记录
    merged_items = []
    i = 0
    while i < len(items):
        current_item = items[i]
        
        # 情况1：当前记录是项目名称首行（*商品名称* 格式）
        if _is_item_name_first_line(current_item):
            merged_item = LineItem()
            merged_item.item_name = current_item.item_name
            # 合并其他字段（如果有）
            merged_item.spec = current_item.spec or ''
            merged_item.unit = current_item.unit or ''
            merged_item.quantity = current_item.quantity or ''
            merged_item.price = current_item.price or ''
            merged_item.amount = current_item.amount or ''
            merged_item.tax_rate = current_item.tax_rate or ''
            merged_item.tax_amount = current_item.tax_amount or ''
            merged_item.source_y = current_item.source_y
            
            # 使用统一合并循环
            j = _merge_continuation_lines(
                merged_item, i, items, pdf_path, layout, lines_dict,
            )
            
            merged_items.append(merged_item)
            start_row = i
            end_row = j - 1
            i = j
            logger.debug(f'{pdf_path}: 合并记录 行{start_row} 到 行{end_row}: 项目名称="{merged_item.item_name[:50]}", 金额="{merged_item.amount}"')
            continue
        
        # 情况2：当前记录有金额，说明是一个完整的商品记录
        if current_item.amount and current_item.amount.strip():
            merged_item = current_item
            if merged_item.source_y is None:
                merged_item.source_y = current_item.source_y
            
            # 使用统一合并循环
            j = _merge_continuation_lines(
                merged_item, i, items, pdf_path, layout, lines_dict,
            )
            
            merged_items.append(merged_item)
            i = j
            continue
        
        # 情况3：当前记录没有金额，使用统一合并循环处理（查找和合并合并为一个循环）
        merged_item = current_item
        
        # 使用统一合并循环（如果merged_item无金额，遇到有金额的行会自动合并）
        j = _merge_continuation_lines(
            merged_item, i, items, pdf_path, layout, lines_dict,
        )
        
        # 判断是否成功合并（有金额或其他有效字段）
        has_amount = merged_item.amount and merged_item.amount.strip()
        has_other_fields = (merged_item.spec or merged_item.unit or merged_item.quantity or 
                           merged_item.price or merged_item.tax_rate or merged_item.tax_amount)
        
        if has_amount or (has_other_fields and merged_item.item_name):
            # 成功合并，添加到结果列表
            merged_items.append(merged_item)
            start_row = i
            end_row = j - 1
            logger.debug(f'{pdf_path}: 合并记录 行{start_row} 到 行{end_row}: 项目名称="{merged_item.item_name[:50] if merged_item.item_name else ""}", 金额="{merged_item.amount}"')
        elif merged_item.item_name:
            if not _should_discard_unmerged_item(merged_item):
                merged_items.append(merged_item)
                logger.debug(
                    f'{pdf_path}: 保留无法合并的记录: 行{i}, '
                    f'项目名称="{merged_item.item_name[:50]}"'
                )
            else:
                logger.debug(
                    f'{pdf_path}: 丢弃无法合并的噪声行: 行{i}, '
                    f'项目名称="{merged_item.item_name[:50]}"'
                )
        
        i = j

    _attach_orphan_suffixes_by_y(merged_items, items, layout, lines_dict, pdf_path)

    for item in merged_items:
        if item.item_name:
            item.item_name = close_item_name_if_paren_only(item.item_name)
    
    # 合并完成后，去掉所有字段中的空格
    for item in merged_items:
        if item.item_name:
            item.item_name = item.item_name.replace(' ', '')
        if item.spec:
            item.spec = item.spec.replace(' ', '')
        if item.unit:
            item.unit = item.unit.replace(' ', '')
        if item.quantity:
            item.quantity = item.quantity.replace(' ', '')
        if item.price:
            item.price = item.price.replace(' ', '')
        if item.amount:
            item.amount = item.amount.replace(' ', '')
        if item.tax_rate:
            item.tax_rate = item.tax_rate.replace(' ', '')
        if item.tax_amount:
            item.tax_amount = item.tax_amount.replace(' ', '')
    
    final_items = []
    for item in merged_items:
        if item.amount and item.amount.strip():
            final_items.append(item)
        else:
            logger.debug(
                f'{pdf_path}: 丢弃无金额的合并结果: '
                f'项目名称="{item.item_name[:50] if item.item_name else ""}"'
            )
    
    return final_items


def can_merge_items(item1: LineItem, item2: LineItem) -> bool:
    """
    判断两个商品记录是否可以合并
    
    合并条件：
    1. item1 缺少某些关键字段（金额、数量、单价等）
    2. item2 有这些字段
    3. 或者 item1 只有项目名称，item2 有其他字段
    
    Args:
        item1: 第一个商品记录
        item2: 第二个商品记录
        
    Returns:
        如果可以合并返回True
    """
    # 如果 item1 有金额且已有项目名称，说明是完整记录，不需要合并
    if item1.amount and item1.amount.strip():
        # 例外：item1 有金额但无项目名称，item2 有项目名称（无金额）→ 互补合并
        if not (item1.item_name and item1.item_name.strip()) and item2.item_name and item2.item_name.strip():
            if not (item2.amount and item2.amount.strip()):
                return True
        return False
    
    # 如果 item2 有金额，且 item1 没有金额，可以合并
    if item2.amount and item2.amount.strip():
        return True
    
    # 如果 item1 只有项目名称，item2 有其他字段（规格、单位、数量等），可以合并
    has_only_name = (item1.item_name and 
                     not item1.spec and not item1.unit and not item1.quantity and 
                     not item1.price and not item1.amount)
    
    has_other_fields = (item2.spec or item2.unit or item2.quantity or 
                       item2.price or item2.tax_rate or item2.tax_amount)
    
    if has_only_name and has_other_fields:
        return True
    
    # 如果 item1 有规格但缺少其他字段，item2 有互补字段，可以合并
    if item1.spec and not item1.amount:
        if item2.amount or item2.quantity or item2.price:
            return True
    
    return False



