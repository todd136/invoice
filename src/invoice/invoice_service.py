"""
发票解析主服务
"""
import os
import logging
import re
from typing import List, Optional, Tuple
import pdfplumber

from .invoice import Invoice, LineItem
from .regex_utils import (
    INVOICE_CODE_REGEX, INVOICE_CODE_REGEX_LOOSE, DATE_REGEX, DATE_REGEX_LOOSE,
    PROJECT_NAME_REGEX, TAX_ID_PATTERNS, TAX_ID_GLOBAL_REGEX, LONG_NUMBER_REGEX,
    ROOM_NUMBER_REGEX, ROOM_ALPHA_REGEX, COMPANY_NAME_REGEX, normalize_alpha_num
)
from .text_utils import clean_garbled_chars
from .number_utils import is_tax_id, score_candidate
from .invoice_partition import (
    partition_invoice_by_lines, 
    print_partition_content, 
    reconstruct_text_from_words,
    group_words_by_y
)

# 获取模块级别的日志记录器
logger = logging.getLogger(__name__)

def extract_invoice_by_table_and_text(pdf_file_path: str) -> Invoice:
    """
    使用 pdfplumber 提取PDF中的发票信息
    
    Args:
        pdf_file_path: PDF 文件路径
        
    Returns:
        Invoice 对象
        
    Raises:
        Exception: 解析失败
    """
    logger.info(f'开始处理发票 {pdf_file_path}...')

    invoice = Invoice()
    invoice.name = os.path.basename(pdf_file_path)

    try:
        with pdfplumber.open(pdf_file_path) as pdf:
            # 电子发票通常只有一页
            page = pdf.pages[0]

            # 1. 提取文本内容（过滤掉印章红色文字）
            words = page.extract_words(
                x_tolerance=5,  # 增大水平容差，帮助合并同一行的字符
                y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=True  # 使用文本流逻辑重组，解决乱序
            )

            # 过滤掉印章文字（红色）
            clean_words = filter_seal_text(page, words)

            # 2. 使用分割线进行发票分区
            logger.info('开始使用分割线进行发票分区...')
            header_words, buyer_words, seller_words, table_words = partition_invoice_by_lines(
                page, clean_words
            )
            # 打印各分区内容（调试用）
            print_partition_content(header_words, buyer_words, seller_words, table_words)

            # 3. 提取发票基本信息（从分区后的内容）
            code, date, buyer, buyer_tax_id, invoice_type = extract_basic_info(
                header_words, buyer_words
            )

            invoice.code = code
            invoice.date = date
            invoice.buyer = buyer
            invoice.buyer_tax_id = buyer_tax_id
            invoice.invoice_type = invoice_type

            # 4. 解析商品明细（从表格区域的单词）
            invoice.items = parse_line_items_from_words(table_words, pdf_file_path)

            # 验证解析结果
            if not invoice.items:
                raise Exception('未能解析到发票明细数据')

            logger.info(f'发票 {pdf_file_path} 解析完成')
            return invoice

    except Exception as e:
        logger.error(f'读取发票 {pdf_file_path} 发生错误: {e}')
        raise


def filter_seal_text(page, words: List[dict]) -> List[dict]:
    """
    过滤掉印章文字（红色）
    
    Args:
        page: pdfplumber Page 对象
        words: 提取的单词列表
        
    Returns:
        过滤后的单词列表
    """
    clean_words = []
    chars = page.chars  # 获取所有字符

    for w in words:
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


def extract_buyer_name_from_words(buyer_words: List[dict]) -> str:
    """从购买方区域提取购买方名称"""
    if not buyer_words:
        return ''
    
    # 重建文本
    buyer_text = reconstruct_text_from_words(buyer_words)
    # 基本清洗
    buyer_text = clean_garbled_chars(buyer_text)
    
    # 策略1：从"名称："后面提取
    name_pattern = re.compile(r'名称[：:]\s*([^\n]+?)(?:\s*(?:统一社会信用代码|纳税人识别号)|$)')
    match = name_pattern.search(buyer_text)
    if match:
        buyer = match.group(1).strip()
        # 如果提取的内容包含关键词，截取到关键词之前
        stop_keywords = ['统一社会信用代码', '纳税人识别号', '销售方', '购买方']
        for kw in stop_keywords:
            if kw in buyer:
                idx = buyer.index(kw)
                if idx > 0:
                    buyer = buyer[:idx].strip()
                    break
        # 排除税号
        if not is_tax_id(buyer) and not re.match(r'^\d{12,}$', buyer):
            return buyer

    # 策略2：匹配公司名称模式
    match = COMPANY_NAME_REGEX.search(buyer_text)
    if match:
        buyer = match.group(1)
        # 如果匹配到的内容包含关键词，截取到关键词之前
        stop_keywords = ['统一社会信用代码', '纳税人识别号', '销售方', '购买方']
        for kw in stop_keywords:
            if kw in buyer:
                idx = buyer.index(kw)
                if idx > 0:
                    buyer = buyer[:idx].strip()
                    break
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


def parse_line_items_from_words(table_words: List[dict], pdf_path: str = '') -> List[LineItem]:
    """
    从表格区域的单词列表中解析明细行
    
    Args:
        table_words: 表格区域的单词列表
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        商品明细列表
    """
    items = []

    if not table_words or len(table_words) == 0:
        logger.warning(f'{pdf_path}: 表格数据为空，无法解析明细')
        return items

    # 将单词按行分组
    lines_dict = group_words_by_y(table_words, y_tolerance=3.0)
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0])

    logger.debug(f'{pdf_path}: 表格共有 {len(sorted_lines)} 行')

    # 找到表头行（包含"项目名称"的行）
    header_row_y = None
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']

    for y, line_words in sorted_lines:
        # 合并整行的文本
        row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])])

        # 查找表头关键词
        keyword_count = sum(1 for kw in table_keywords if kw in row_text)
        if keyword_count >= 3:  # 至少包含3个表头关键词
            header_row_y = y
            logger.debug(f'{pdf_path}: 找到表头行，Y={y:.2f}, 内容: {row_text[:100]}')
            break

    if header_row_y is None:
        logger.warning(f'{pdf_path}: 未找到表头行（需要包含至少3个表头关键词）')
        # 打印前几行内容帮助调试
        for i, (y, line_words) in enumerate(sorted_lines[:5]):
            row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])])
            logger.debug(f'{pdf_path}: 表格第 {i} 行 (Y={y:.2f}): {row_text[:100]}')
        return items

    # 识别各列的X坐标范围（通过表头行的单词位置）
    # 先找到每个关键词对应的所有单词（可能被拆分成多个）
    keyword_words = {}  # {keyword: [word1, word2, ...]}
    header_line_words = sorted(lines_dict[header_row_y], key=lambda w: w['x0'])
    
    # 打印表头行的x0坐标信息
    logger.debug(f'{pdf_path}: ================================================================================')
    logger.debug(f'{pdf_path}: 表头行 (Y={header_row_y:.2f}) X坐标信息:')
    logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
    for word in header_line_words:
        word_text = clean_garbled_chars(word['text'])
        logger.debug(f'{pdf_path}:   单词: "{word_text:20s}" | x0={word["x0"]:8.2f} | x1={word["x1"]:8.2f} | 中心={((word["x0"]+word["x1"])/2):8.2f}')
    
    # 收集每个关键词对应的所有单词
    # 方法1：直接匹配（关键词完整出现在单词中）
    for word in header_line_words:
        word_text = clean_garbled_chars(word['text'])
        for keyword in table_keywords:
            if keyword in word_text:
                if keyword not in keyword_words:
                    keyword_words[keyword] = []
                keyword_words[keyword].append(word)
    
    # 方法2：处理被拆分的关键词（如"单位"被拆成"单"和"位"）
    # 对于每个关键词，找到所有包含其字符的单词，然后根据X坐标连续性判断
    for keyword in table_keywords:
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
                    # 检查X坐标是否相邻（距离小于30像素）
                    distance = abs(w2['x0'] - w1['x1'])
                    if distance < 30:
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
    
    # 为每个关键词计算列的X坐标范围（包含所有相关单词）
    col_x_ranges = {}
    col_x_starts = {}  # 记录每列的首字x0坐标（用于匹配明细数据）
    for keyword, words in keyword_words.items():
        if not words:
            continue
        # 找到该关键词所有单词的最小x0和最大x1
        min_x0 = min(w['x0'] for w in words)
        max_x1 = max(w['x1'] for w in words)
        # 扩展范围：左右各扩展10像素，以便匹配同一列的其他单词（减少扩展范围，避免跨列匹配）
        col_x_ranges[keyword] = (min_x0 - 10, max_x1 + 10)
        col_x_starts[keyword] = min_x0  # 记录首字x0坐标
        logger.debug(f'{pdf_path}:   列"{keyword}": 包含{len(words)}个单词, x0范围=[{min_x0:.2f}, {max_x1:.2f}], 扩展后=[{min_x0-10:.2f}, {max_x1+10:.2f}]')
    
    logger.debug(f'{pdf_path}: ================================================================================')

    # 遍历数据行（表头行之后的行）
    # 第一步：识别所有可能的数据行，并提取字段
    raw_items = []  # 存储每行解析出的原始item
    header_found = False
    
    for y, line_words in sorted_lines:
        # 跳过表头行
        if y == header_row_y:
            header_found = True
            continue
        if not header_found:
            continue
        
        if not line_words:
            continue

        # 检查是否是合计行或其他非明细行
        row_text = ' '.join([w['text'] for w in sorted(line_words, key=lambda w: w['x0'])]).lower()
        if any(kw in row_text for kw in ['合计', '价税合计', '备注', '开票人']):
            continue

        # 按X坐标排序单词
        sorted_line_words = sorted(line_words, key=lambda w: w['x0'])
        
        # 提取字段（基于列的位置）
        def find_word_in_column(keyword: str) -> Optional[str]:
            """在指定列中查找单词，返回该列中所有匹配单词的合并文本"""
            if keyword not in col_x_ranges:
                return None
            col_x0, col_x1 = col_x_ranges[keyword]
            # 计算列的中心点（用于选择最匹配的列）
            col_center = (col_x0 + col_x1) / 2
            
            # 查找X坐标在列范围内的所有单词
            matched_words = []
            for word in sorted_line_words:
                word_center = (word['x0'] + word['x1']) / 2
                # 优先使用单词中心点判断，确保更精确的匹配
                # 只有当中心点在列范围内时，才认为该单词属于该列
                if col_x0 <= word_center <= col_x1:
                    # 检查该单词是否也匹配到其他列（避免跨列匹配）
                    matched_other_columns = []
                    for other_keyword, (other_x0, other_x1) in col_x_ranges.items():
                        if other_keyword == keyword:
                            continue
                        if other_x0 <= word_center <= other_x1:
                            other_col_center = (other_x0 + other_x1) / 2
                            matched_other_columns.append((other_keyword, other_col_center))
                    
                    # 如果单词匹配到多个列，选择中心点最接近的列
                    if matched_other_columns:
                        # 计算到当前列和其他列的距离
                        current_distance = abs(word_center - col_center)
                        other_distances = [(kw, abs(word_center - oc)) for kw, oc in matched_other_columns]
                        min_other_distance = min(d for _, d in other_distances)
                        
                        # 如果当前列不是最接近的，跳过该单词
                        if current_distance > min_other_distance:
                            continue
                    
                    matched_words.append(word)
            
            if not matched_words:
                return None
            
            # 合并该列中的所有单词（按X坐标排序）
            matched_words.sort(key=lambda w: w['x0'])
            result = ' '.join([clean_garbled_chars(w['text']).strip() for w in matched_words])
            return result.strip() if result.strip() else None
        
        # 查找项目名称（优先查找最左侧包含中文的单词，或在项目名称列中查找）
        item_name = ''
        
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

        # 创建明细项（即使没有项目名称，也可能有其他字段，需要保留用于合并）
        item = LineItem()
        item.item_name = item_name
        item.spec = find_word_in_column('规格型号') or ''
        item.unit = find_word_in_column('单位') or ''
        item.quantity = find_word_in_column('数量') or ''
        item.price = find_word_in_column('单价') or ''
        item.amount = find_word_in_column('金额') or ''
        item.tax_rate = find_word_in_column('税率') or ''
        item.tax_amount = find_word_in_column('税额') or ''
        
        # 如果这一行有任何字段，就保留（可能是明细行的一部分）
        if item.item_name or item.spec or item.unit or item.quantity or item.price or item.amount:
            raw_items.append(item)
            
            # 打印明细数据行的x0坐标信息
            logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
            logger.debug(f'{pdf_path}: 明细数据行 (Y={y:.2f}) X坐标信息:')
            for word in sorted_line_words:
                word_text = clean_garbled_chars(word['text'])
                # 判断该单词属于哪一列
                matched_columns = []
                for keyword, (col_x0, col_x1) in col_x_ranges.items():
                    word_center = (word['x0'] + word['x1']) / 2
                    if col_x0 <= word_center <= col_x1:
                        matched_columns.append(keyword)
                
                column_info = f' -> [{", ".join(matched_columns)}]' if matched_columns else ' -> [未匹配到列]'
                logger.debug(f'{pdf_path}:   单词: "{word_text:20s}" | x0={word["x0"]:8.2f} | x1={word["x1"]:8.2f} | 中心={((word["x0"]+word["x1"])/2):8.2f}{column_info}')

            logger.debug(f'{pdf_path}: 解析结果: 项目名称="{item.item_name}", 金额="{item.amount}", 规格="{item.spec}", 单位="{item.unit}", 数量="{item.quantity}", 单价="{item.price}", 税率="{item.tax_rate}", 税额="{item.tax_amount}"')
            logger.debug(f'{pdf_path}: --------------------------------------------------------------------------------')
    
    # 第二步：合并跨行的商品记录
    items = merge_split_line_items(raw_items, col_x_starts, pdf_path)

    logger.info(f'{pdf_path}: 合并后共有 {len(items)} 条明细')

    if len(items) == 0 and len(raw_items) > 0:
        logger.warning(f'{pdf_path}: 处理了 {len(raw_items)} 行数据，但未能解析到任何商品明细')

    return items


def merge_split_line_items(items: List[LineItem], col_x_starts: dict, pdf_path: str = '') -> List[LineItem]:
    """
    合并跨行的商品记录
    
    策略：
    1. 根据金额字段判断实际商品数量（有金额的记录才算一个商品）
    2. 根据字段互补关系合并行（如果一条记录缺少某些字段，而下一条记录有这些字段，则合并）
    3. 根据X坐标判断字段是否属于同一列（用于合并折行的规格等字段）
    
    Args:
        items: 初始解析的商品明细列表
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
    
    # 如果实际商品数量等于初始记录数，说明没有跨行问题
    if actual_item_count == len(items):
        return items
    
    # 合并逻辑：遍历items，合并互补的记录
    merged_items = []
    i = 0
    while i < len(items):
        current_item = items[i]
        
        # 如果当前记录有金额，说明是一个完整的商品记录
        if current_item.amount and current_item.amount.strip():
            # 检查后续行是否有需要合并的字段（如折行的规格、项目名称等）
            merged_item = current_item
            j = i + 1
            while j < len(items) and j < i + 4:  # 最多向后查找3行
                next_item = items[j]
                # 如果下一行有金额，说明是新的商品，停止合并
                if next_item.amount and next_item.amount.strip():
                    break
                
                # 检查是否是折行的字段（只有某个字段，没有其他字段）
                has_only_spec = (next_item.spec and not next_item.item_name and not next_item.amount and 
                                not next_item.unit and not next_item.quantity and not next_item.price)
                has_only_item_name = (next_item.item_name and not next_item.spec and not next_item.amount and 
                                     not next_item.unit and not next_item.quantity and not next_item.price)
                
                if has_only_spec:
                    # 合并规格
                    if merged_item.spec:
                        merged_item.spec = f'{merged_item.spec} {next_item.spec}'.strip()
                    else:
                        merged_item.spec = next_item.spec
                    logger.debug(f'{pdf_path}: 合并规格字段: 行{i} + 行{j}, 规格="{merged_item.spec}"')
                    j += 1
                elif has_only_item_name:
                    # 合并项目名称（项目名称折行）
                    if merged_item.item_name:
                        merged_item.item_name = f'{merged_item.item_name}{next_item.item_name}'.strip()
                    else:
                        merged_item.item_name = next_item.item_name
                    logger.debug(f'{pdf_path}: 合并项目名称字段: 行{i} + 行{j}, 项目名称="{merged_item.item_name}"')
                    j += 1
                else:
                    break
            merged_items.append(merged_item)
            i = j
            continue
        
        # 如果当前记录没有金额，尝试与后续记录合并
        # 查找下一个有金额的记录，或者下一个有互补字段的记录
        merged = False
        for j in range(i + 1, min(i + 4, len(items))):  # 最多向前查找3行
            next_item = items[j]
            
            # 检查是否可以合并
            if can_merge_items(current_item, next_item):
                # 合并两个记录
                merged_item = merge_two_items(current_item, next_item)
                
                # 继续检查是否有更多需要合并的行（如折行的规格、项目名称等）
                k = j + 1
                while k < len(items) and k < j + 4:
                    more_item = items[k]
                    # 如果下一行有金额，说明是新的商品，停止合并
                    if more_item.amount and more_item.amount.strip():
                        break
                    
                    # 检查是否是折行的字段
                    has_only_spec = (more_item.spec and not more_item.item_name and not more_item.amount and
                                    not more_item.unit and not more_item.quantity and not more_item.price)
                    has_only_item_name = (more_item.item_name and not more_item.spec and not more_item.amount and
                                         not more_item.unit and not more_item.quantity and not more_item.price)
                    
                    if has_only_spec:
                        if merged_item.spec:
                            merged_item.spec = f'{merged_item.spec} {more_item.spec}'.strip()
                        else:
                            merged_item.spec = more_item.spec
                        logger.debug(f'{pdf_path}: 合并规格字段: 行{i}+行{j} + 行{k}, 规格="{merged_item.spec}"')
                        k += 1
                    elif has_only_item_name:
                        # 合并项目名称（项目名称折行）
                        if merged_item.item_name:
                            merged_item.item_name = f'{merged_item.item_name}{more_item.item_name}'.strip()
                        else:
                            merged_item.item_name = more_item.item_name
                        logger.debug(f'{pdf_path}: 合并项目名称字段: 行{i}+行{j} + 行{k}, 项目名称="{merged_item.item_name}"')
                        k += 1
                    else:
                        break
                
                merged_items.append(merged_item)
                i = k  # 跳过已合并的记录
                merged = True
                logger.debug(f'{pdf_path}: 合并记录 行{i-k+j} 到 行{k-1}: 项目名称="{merged_item.item_name[:30]}", 金额="{merged_item.amount}"')
                break
        
        if not merged:
            # 如果无法合并，保留当前记录（可能是只有项目名称的记录）
            if current_item.item_name:
                merged_items.append(current_item)
            i += 1
    
    return merged_items


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
    # 如果 item1 有金额，说明是完整记录，不需要合并
    if item1.amount and item1.amount.strip():
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


def merge_two_items(item1: LineItem, item2: LineItem) -> LineItem:
    """
    合并两个商品记录
    
    Args:
        item1: 第一个商品记录
        item2: 第二个商品记录
        
    Returns:
        合并后的商品记录
    """
    merged = LineItem()
    
    # 项目名称：优先使用 item1 的（通常是第一行）
    merged.item_name = item1.item_name or item2.item_name
    
    # 规格型号：合并两个记录的规格（可能是折行显示）
    spec1 = (item1.spec or '').strip()
    spec2 = (item2.spec or '').strip()
    if spec1 and spec2:
        # 如果两个都有规格，合并（可能是折行）
        merged.spec = f'{spec1} {spec2}'.strip()
    else:
        merged.spec = spec1 or spec2
    
    # 其他字段：优先使用有值的字段
    merged.unit = item1.unit or item2.unit
    merged.quantity = item1.quantity or item2.quantity
    merged.price = item1.price or item2.price
    merged.amount = item1.amount or item2.amount
    merged.tax_rate = item1.tax_rate or item2.tax_rate
    merged.tax_amount = item1.tax_amount or item2.tax_amount
    
    return merged




