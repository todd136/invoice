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
from .text_utils import clean_text, is_all_text_valid, is_contain_garbled_text, clean_garbled_chars
from .number_utils import is_tax_id, score_candidate
from .invoice_partition import partition_invoice_by_lines, print_partition_content


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
    logging.info(f'开始处理发票 {pdf_file_path}...')

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
            logging.info('开始使用分割线进行发票分区...')
            try:
                header_words, buyer_words, seller_words, table_words = partition_invoice_by_lines(
                    page, clean_words
                )
                # 打印各分区内容
                print_partition_content(header_words, buyer_words, seller_words, table_words)
            except Exception as e:
                logging.warning(f'分割线分区失败: {e}')

            # 3. 提取表格数据（明细栏）
            # table = page.extract_table(table_settings={
            #     "vertical_strategy": "text",
            #     "horizontal_strategy": "text",
            #     "snap_x_tolerance": 8,
            #     "snap_y_tolerance": 3,
            #     "join_tolerance": 15,
            #     "min_words_vertical": 0,
            # })
            #
            # # 4. 构造完整文本
            # full_text = ' '.join([w['text'] for w in clean_words])
            #
            # # 5. 文本清洗与验证
            # cleaned_text = clean_text(full_text)
            # if not is_all_text_valid(cleaned_text):
            #     raise Exception('无法从PDF中提取有效文本，可能是编码问题')
            # if is_contain_garbled_text(cleaned_text):
            #     raise Exception('提取的文本可能是乱码。这可能是因为 PDF 文件使用了特殊的字体编码')
            #
            # # 6. 提取发票基本信息
            # code, date, buyer, buyer_tax_id, invoice_type = extract_basic_info(
            #     clean_words, cleaned_text, full_text
            # )
            #
            # invoice.code = code
            # invoice.date = date
            # invoice.buyer = buyer
            # invoice.buyer_tax_id = buyer_tax_id
            # invoice.invoice_type = invoice_type
            #
            # # 7. 解析商品明细
            # logging.debug(f'开始解析表格数据，table 是否为空: {table is None or len(table) == 0 if table else "None"}')
            # if table:
            #     logging.debug(f'表格行数: {len(table)}')
            #     if len(table) > 0:
            #         logging.debug(f'表格第一行: {table[0]}')
            #
            # invoice.items = parse_line_items_from_table(table, cleaned_text, pdf_file_path)
            #
            # # 验证解析结果
            # if not invoice.items:
            #     logging.warning(f'未能从表格中解析到发票明细数据，尝试从文本中提取...')
            #     # 尝试从文本中提取明细（备选方案）
            #     invoice.items = parse_line_items_from_text(cleaned_text, full_text)
            #
            # if not invoice.items:
            #     raise Exception('未能解析到发票明细数据')

            logging.info(f'发票 {pdf_file_path} 解析完成')
            return invoice

    except Exception as e:
        logging.error(f'读取发票 {pdf_file_path} 发生错误: {e}')
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
    words: List[dict],
    cleaned_text: str,
    full_text: str
) -> Tuple[str, str, str, str, str]:
    """
    提取发票基本信息
    
    Args:
        words: 过滤后的单词列表
        cleaned_text: 清洗后的文本
        full_text: 完整文本
        
    Returns:
        (code, date, buyer, buyer_tax_id, invoice_type)
    """
    # 1. 提取发票号码
    code = extract_invoice_code(cleaned_text, full_text)

    # 2. 提取开票日期
    date = extract_date(cleaned_text, full_text)

    # 3. 提取购买方名称
    buyer = extract_buyer_name(words, cleaned_text, full_text)

    # 4. 提取购买方税号
    buyer_tax_id = extract_buyer_tax_id(words, cleaned_text, full_text)

    # 5. 提取发票类型
    invoice_type = extract_invoice_type(cleaned_text, full_text)

    return code, date, buyer, buyer_tax_id, invoice_type


def extract_invoice_code(cleaned_text: str, full_text: str) -> str:
    """提取发票号码"""
    # 优先从 cleaned_text 中提取
    match = INVOICE_CODE_REGEX.search(cleaned_text.replace(' ', ''))
    if match:
        return match.group(1)

    # 回退：使用宽松模式
    match = INVOICE_CODE_REGEX_LOOSE.search(cleaned_text)
    if match:
        return match.group(1)

    # 最后回退：查找所有长数字
    nums = LONG_NUMBER_REGEX.findall(cleaned_text.replace(' ', ''))
    if nums:
        # 选择最长的 12-20 位数字
        valid_nums = [n for n in nums if 12 <= len(n) <= 20]
        if valid_nums:
            return max(valid_nums, key=len)

    return ''


def extract_date(cleaned_text: str, full_text: str) -> str:
    """提取开票日期"""
    # 优先从 cleaned_text 中提取
    match = DATE_REGEX.search(cleaned_text)
    if match:
        return match.group(1)

    # 回退：使用宽松模式
    match = DATE_REGEX_LOOSE.search(cleaned_text)
    if match:
        return match.group().replace(' ', '')

    return ''


def extract_buyer_name(words: List[dict], cleaned_text: str, full_text: str) -> str:
    """提取购买方名称"""
    # 策略1：从"名称："后面提取
    name_pattern = re.compile(r'名称[：:]\s*([^\n]+?)(?:\s*(?:统一社会信用代码|纳税人识别号)|$)')
    match = name_pattern.search(cleaned_text)
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
    match = COMPANY_NAME_REGEX.search(cleaned_text)
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
    match = ROOM_NUMBER_REGEX.search(cleaned_text)
    if match:
        return match.group()

    match = ROOM_ALPHA_REGEX.search(cleaned_text)
    if match:
        return match.group()

    # 策略4：查找最长的中文文本（排除税号等）
    cleaned = re.sub(r'\d{12,}', '', cleaned_text)
    chinese_texts = re.findall(r'[\u4e00-\u9fa5]{3,}', cleaned)
    if chinese_texts:
        # 排除包含排除关键词的
        exclude_keywords = ['统一社会信用代码', '纳税人识别号', '购买方', '销售方']
        valid_texts = [t for t in chinese_texts if not any(kw in t for kw in exclude_keywords)]
        if valid_texts:
            return max(valid_texts, key=len)

    return ''


def extract_buyer_tax_id(words: List[dict], cleaned_text: str, full_text: str) -> str:
    """提取购买方统一社会信用代码/纳税人识别号"""
    # 收集候选税号
    candidates = []

    # 1. 从模式匹配中收集
    for pattern in TAX_ID_PATTERNS:
        matches = pattern.findall(cleaned_text)
        candidates.extend(matches)

    # 2. 全局扫描（去空格后）
    cleaned_norm = normalize_alpha_num(cleaned_text)
    global_matches = TAX_ID_GLOBAL_REGEX.findall(cleaned_norm)
    candidates.extend(global_matches)

    # 3. 从 full_text 中收集（原始文本，不去空格）
    for pattern in TAX_ID_PATTERNS:
        matches = pattern.findall(full_text)
        candidates.extend(matches)

    # 4. 验证和评分
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

    # 5. 选择得分最高的
    if valid_candidates:
        valid_candidates.sort(key=lambda x: x[1], reverse=True)
        return valid_candidates[0][0]

    return ''


def extract_invoice_type(cleaned_text: str, full_text: str) -> str:
    """提取发票类型"""
    # 优先从 cleaned_text 中识别
    if '（普通发票）' in cleaned_text or '(普通发票)' in cleaned_text:
        return '普'
    if '（增值税专用发票）' in cleaned_text or '(增值税专用发票)' in cleaned_text:
        return '专'

    # 如果没找到完整格式，尝试查找关键词
    has_putong = '普通' in cleaned_text or '普' in cleaned_text
    has_fapiao = '发票' in cleaned_text
    if has_putong and has_fapiao:
        return '普'
    if '增值税专用发票' in cleaned_text or '专用发票' in cleaned_text:
        return '专'
    if '增值税' in cleaned_text and '专用' in cleaned_text and has_fapiao:
        return '专'

    return ''


def parse_line_items_from_table(table: Optional[List[List]], cleaned_text: str, pdf_path: str = '') -> List[LineItem]:
    """
    从表格中解析明细行
    
    Args:
        table: pdfplumber 提取的表格数据
        cleaned_text: 清洗后的文本（用于参考）
        pdf_path: PDF 文件路径（用于日志）
        
    Returns:
        商品明细列表
    """
    items = []

    if not table or len(table) == 0:
        logging.warning(f'{pdf_path}: 表格数据为空，无法解析明细')
        return items

    logging.debug(f'{pdf_path}: 表格共有 {len(table)} 行')

    # 找到表头行（包含"项目名称"的行）
    header_row_idx = -1
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']

    for i, row in enumerate(table):
        if not row:
            continue
        # 合并整行的文本
        row_text = ' '.join(str(cell) if cell else '' for cell in row)

        # 查找表头关键词
        keyword_count = sum(1 for kw in table_keywords if kw in row_text)
        if keyword_count >= 3:  # 至少包含3个表头关键词
            header_row_idx = i
            logging.debug(f'{pdf_path}: 找到表头行，索引: {i}, 内容: {row_text[:100]}')
            break

    if header_row_idx < 0:
        logging.warning(f'{pdf_path}: 未找到表头行（需要包含至少3个表头关键词）')
        # 打印前几行内容帮助调试
        for i, row in enumerate(table[:5]):
            if row:
                row_text = ' '.join(str(cell) if cell else '' for cell in row)
                logging.debug(f'{pdf_path}: 表格第 {i} 行: {row_text[:100]}')
        return items

    # 识别各列的索引（通过表头行的内容）
    col_indices = {}
    header_row = table[header_row_idx]
    for col_idx, cell in enumerate(header_row):
        if not cell:
            continue
        cell_text = str(cell).strip()
        for keyword in table_keywords:
            if keyword in cell_text and keyword not in col_indices:
                col_indices[keyword] = col_idx
                break

    # 遍历数据行（表头行之后的行）
    data_row_count = 0
    for i in range(header_row_idx + 1, len(table)):
        row = table[i]
        if not row or len(row) == 0:
            continue

        # 检查是否是合计行或其他非明细行
        row_text = ' '.join(str(cell) if cell else '' for cell in row).lower()
        if any(kw in row_text for kw in ['合计', '价税合计', '备注', '开票人', '¥']):
            continue

        data_row_count += 1
        # 检查是否是明细行（包含项目名称模式）
        item_name = ''
        item_name_col_idx = col_indices.get('项目名称', 0)

        # 优先检查列0
        if 0 < len(row) and row[0]:
            candidate_text = clean_garbled_chars(str(row[0]))
            # 首先尝试严格匹配（*项目名称*费用名称）
            if PROJECT_NAME_REGEX.search(candidate_text):
                item_name = candidate_text.strip()
                item_name_col_idx = 0
            # 如果严格匹配失败，尝试放宽条件：包含中文且不是纯数字
            elif not item_name and re.search(r'[\u4e00-\u9fa5]', candidate_text) and not re.match(r'^[\d\s\.]+$', candidate_text):
                # 检查是否可能是项目名称（长度合理，包含中文）
                if len(candidate_text.strip()) >= 2 and len(candidate_text.strip()) <= 100:
                    item_name = candidate_text.strip()
                    item_name_col_idx = 0
                    logging.debug(f'{pdf_path}: 使用放宽条件匹配到项目名称: {item_name[:50]}')
        
        # 如果列0没有匹配，使用 col_indices 指定的列
        if not item_name and item_name_col_idx < len(row) and row[item_name_col_idx]:
            candidate_text = clean_garbled_chars(str(row[item_name_col_idx]))
            # 首先尝试严格匹配
            if PROJECT_NAME_REGEX.search(candidate_text):
                item_name = candidate_text.strip()
            # 放宽条件
            elif re.search(r'[\u4e00-\u9fa5]', candidate_text) and not re.match(r'^[\d\s\.]+$', candidate_text):
                if len(candidate_text.strip()) >= 2 and len(candidate_text.strip()) <= 100:
                    item_name = candidate_text.strip()
                    logging.debug(f'{pdf_path}: 使用放宽条件匹配到项目名称: {item_name[:50]}')

        if not item_name:
            continue

        # 创建明细项
        item = LineItem()
        item.item_name = item_name

        # 提取其他字段
        if '规格型号' in col_indices:
            col_idx = col_indices['规格型号']
            if col_idx < len(row) and row[col_idx]:
                item.spec = clean_garbled_chars(str(row[col_idx])).strip()

        if '单位' in col_indices:
            col_idx = col_indices['单位']
            if col_idx < len(row) and row[col_idx]:
                item.unit = clean_garbled_chars(str(row[col_idx])).strip()

        if '数量' in col_indices:
            col_idx = col_indices['数量']
            if col_idx < len(row) and row[col_idx]:
                item.quantity = clean_garbled_chars(str(row[col_idx])).strip()

        if '单价' in col_indices:
            col_idx = col_indices['单价']
            if col_idx < len(row) and row[col_idx]:
                item.price = clean_garbled_chars(str(row[col_idx])).strip()

        if '金额' in col_indices:
            col_idx = col_indices['金额']
            if col_idx < len(row) and row[col_idx]:
                item.amount = clean_garbled_chars(str(row[col_idx])).strip()

        if '税率' in col_indices:
            col_idx = col_indices['税率']
            if col_idx < len(row) and row[col_idx]:
                item.tax_rate = clean_garbled_chars(str(row[col_idx])).strip()

        if '税额' in col_indices:
            col_idx = col_indices['税额']
            if col_idx < len(row) and row[col_idx]:
                item.tax_amount = clean_garbled_chars(str(row[col_idx])).strip()

        items.append(item)

    logging.info(f'{pdf_path}: 从表格中解析到 {len(items)} 条明细，共处理 {data_row_count} 行数据')
    if len(items) == 0 and data_row_count > 0:
        logging.warning(f'{pdf_path}: 处理了 {data_row_count} 行数据，但未能匹配到任何项目名称')
        # 打印前几行数据帮助调试
        for i in range(header_row_idx + 1, min(header_row_idx + 4, len(table))):
            row = table[i]
            if row:
                row_text = ' '.join(str(cell) if cell else '' for cell in row)
                logging.debug(f'{pdf_path}: 数据行 {i}: {row_text[:150]}')

    return items


def parse_line_items_from_text(cleaned_text: str, full_text: str) -> List[LineItem]:
    """
    从文本中解析明细行（备选方案，当表格提取失败时使用）
    
    Args:
        cleaned_text: 清洗后的文本
        full_text: 完整文本
        
    Returns:
        商品明细列表
    """
    items = []
    
    # 尝试从文本中查找项目名称模式
    # 使用更宽松的正则，匹配 *项目名称*费用名称 格式
    matches = PROJECT_NAME_REGEX.findall(cleaned_text)
    
    if not matches:
        # 如果找不到标准格式，尝试查找包含"项目"关键词的行
        lines = cleaned_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 查找包含"项目"且长度合理的行
            if '项目' in line and len(line) > 5 and len(line) < 200:
                # 检查是否包含项目名称模式
                if PROJECT_NAME_REGEX.search(line):
                    matches.append(line)
    
    if not matches:
        logging.warning('从文本中也未能找到项目明细')
        return items
    
    # 为每个匹配创建明细项
    for match in matches:
        item = LineItem()
        item.item_name = match.strip()
        items.append(item)
        logging.debug(f'从文本中提取到项目: {item.item_name[:50]}')
    
    logging.info(f'从文本中解析到 {len(items)} 条明细')
    return items


