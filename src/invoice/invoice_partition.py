"""
发票分区服务
基于分割线将发票分为不同区域（发票头、购买方、销售方、表格）
"""
import logging
import math
from typing import List, Optional, Tuple


def find_header_bottom_line(lines: List[dict], words: List[dict], page_width: float) -> Optional[dict]:
    """
    查找发票头与购销方的分界线（水平线）
    
    Args:
        lines: 页面中的所有线条
        words: 提取的单词列表
        page_width: 页面宽度
    
    Returns:
        分界线对象，如果未找到返回None
    """
    header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票']
    
    # 找到发票头关键词的Y坐标范围
    header_y_coords = []
    for word in words:
        if any(kw in word['text'] for kw in header_keywords):
            header_y_coords.append(word['top'])
    
    if not header_y_coords:
        logging.debug('未找到发票头关键词，无法识别发票头底部分割线')
        return None
    
    header_y_min = min(header_y_coords)  # 最下方的关键词
    
    # 查找在关键词下方、横跨页面的水平线
    candidates = []
    for line in lines:
        # 水平线（y0和y1接近）
        if abs(line['y0'] - line['y1']) < 2.0:
            line_width = abs(line['x1'] - line['x0'])
            # 横跨大部分页面（>70%）
            if line_width > page_width * 0.7:
                # 在关键词下方（容差20像素）
                if header_y_min - 30 < line['y0'] < header_y_min + 10:
                    candidates.append(line)
    
    if candidates:
        # 选择最接近关键词的线
        candidates.sort(key=lambda l: abs(l['y0'] - header_y_min))
        logging.debug(f'找到发票头底部分割线，Y坐标: {candidates[0]["y0"]:.2f}')
        return candidates[0]
    
    logging.debug('未找到发票头底部分割线')
    return None


def find_table_top_line(lines: List[dict], words: List[dict], page_width: float) -> Optional[dict]:
    """
    查找购销方与表格的分界线（水平线）
    
    Args:
        lines: 页面中的所有线条
        words: 提取的单词列表
        page_width: 页面宽度
    
    Returns:
        分界线对象，如果未找到返回None
    """
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']
    
    # 找到表头关键词的Y坐标
    table_header_y = None
    for word in words:
        if any(kw in word['text'] for kw in table_keywords):
            if table_header_y is None or word['top'] > table_header_y:
                table_header_y = word['top']
    
    if not table_header_y:
        logging.debug('未找到表格表头关键词，无法识别表格顶部分割线')
        return None
    
    # 查找在表头上方、横跨页面的水平线
    candidates = []
    for line in lines:
        if abs(line['y0'] - line['y1']) < 2.0:  # 水平线
            line_width = abs(line['x1'] - line['x0'])
            if line_width > page_width * 0.7:
                # 在表头上方（容差20像素）
                if table_header_y - 20 < line['y0'] < table_header_y + 30:
                    candidates.append(line)
    
    if candidates:
        candidates.sort(key=lambda l: abs(l['y0'] - table_header_y))
        logging.debug(f'找到表格顶部分割线，Y坐标: {candidates[0]["y0"]:.2f}')
        return candidates[0]
    
    logging.debug('未找到表格顶部分割线')
    return None


def find_buyer_seller_divider(
    lines: List[dict], 
    header_bottom_line: Optional[dict], 
    table_top_line: Optional[dict], 
    page_width: float
) -> Optional[dict]:
    """
    查找购买方与销售方的分界线（垂直线）
    
    Args:
        lines: 页面中的所有线条
        header_bottom_line: 发票头底部分割线
        table_top_line: 表格顶部分割线
        page_width: 页面宽度
    
    Returns:
        分界线对象，如果未找到返回None
    """
    if not header_bottom_line or not table_top_line:
        return None
    
    mid_x = page_width / 2
    y_range = (table_top_line['y0'], header_bottom_line['y0'])
    
    candidates = []
    for line in lines:
        # 垂直线（x0和x1接近）
        if abs(line['x0'] - line['x1']) < 2.0:
            # 在页面中间位置（±15%容差）
            if abs(line['x0'] - mid_x) < page_width * 0.15:
                # 在购销方区域内
                line_y_range = (min(line['y0'], line['y1']), 
                               max(line['y0'], line['y1']))
                if (line_y_range[0] < y_range[1] and 
                    line_y_range[1] > y_range[0]):
                    candidates.append(line)
    
    if candidates:
        # 选择最接近页面中点的线
        candidates.sort(key=lambda l: abs(l['x0'] - mid_x))
        logging.debug(f'找到购买方/销售方分界线，X坐标: {candidates[0]["x0"]:.2f}')
        return candidates[0]
    
    logging.debug('未找到购买方/销售方分界线')
    return None


def filter_relevant_lines(lines: List[dict], page_width: float) -> List[dict]:
    """
    过滤出关键分割线，排除无关线条
    
    Args:
        lines: 页面中的所有线条
        page_width: 页面宽度
    
    Returns:
        过滤后的线条列表
    """
    relevant_lines = []
    
    for line in lines:
        # 计算线条长度
        line_length = math.sqrt(
            (line['x1'] - line['x0'])**2 + (line['y1'] - line['y0'])**2
        )
        
        # 1. 过滤太短的线（可能是装饰）
        if line_length < page_width * 0.3:
            continue
        
        # 2. 过滤太细的线（可能是表格内部线，但保留细线作为备选）
        # linewidth可能不存在，跳过这个检查
        
        # 3. 过滤红色线条（可能是印章）
        color = line.get('stroking_color')
        if color and len(color) == 3:
            if color[0] > 0.6 and color[1] < 0.4 and color[2] < 0.4:
                continue  # 红色线条，可能是印章
        
        relevant_lines.append(line)
    
    return relevant_lines


def reconstruct_text_from_words(words: List[dict]) -> str:
    """
    从单词列表重建文本（按Y-X排序）
    
    Args:
        words: 单词列表
    
    Returns:
        重建后的文本
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
    
    # 按Y坐标排序（从上到下，Y坐标越大越靠上）
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0], reverse=True)
    
    # 每行内按X坐标排序（从左到右）
    result_lines = []
    for y, line_words in sorted_lines:
        sorted_words = sorted(line_words, key=lambda w: w['x0'])
        line_text = ' '.join([w['text'] for w in sorted_words])
        result_lines.append(line_text)
    
    return '\n'.join(result_lines)


def partition_invoice_by_lines(page, words: List[dict]) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """
    通过分割线将发票分为4个区域
    
    Args:
        page: pdfplumber Page对象
        words: 提取的单词列表（已过滤印章）
    
    Returns:
        (header_words, buyer_words, seller_words, table_words)
    """
    # 获取页面尺寸
    page_width = page.width
    page_height = page.height
    
    # 提取所有线条
    all_lines = page.lines
    logging.info(f'页面中共有 {len(all_lines)} 条线条')
    
    # 过滤无关线条
    lines = filter_relevant_lines(all_lines, page_width)
    logging.info(f'过滤后剩余 {len(lines)} 条相关线条')
    
    # 识别关键分割线
    header_bottom_line = find_header_bottom_line(lines, words, page_width)
    table_top_line = find_table_top_line(lines, words, page_width)
    buyer_seller_divider = find_buyer_seller_divider(
        lines, header_bottom_line, table_top_line, page_width
    )
    
    # 打印分割线信息
    logging.info('=== 分割线识别结果 ===')
    if header_bottom_line:
        logging.info(f'发票头底部分割线: Y={header_bottom_line["y0"]:.2f}, '
                    f'X范围=[{header_bottom_line["x0"]:.2f}, {header_bottom_line["x1"]:.2f}]')
    else:
        logging.warning('未找到发票头底部分割线')
    
    if table_top_line:
        logging.info(f'表格顶部分割线: Y={table_top_line["y0"]:.2f}, '
                    f'X范围=[{table_top_line["x0"]:.2f}, {table_top_line["x1"]:.2f}]')
    else:
        logging.warning('未找到表格顶部分割线')
    
    if buyer_seller_divider:
        logging.info(f'购买方/销售方分界线: X={buyer_seller_divider["x0"]:.2f}, '
                    f'Y范围=[{buyer_seller_divider["y0"]:.2f}, {buyer_seller_divider["y1"]:.2f}]')
    else:
        logging.info('未找到购买方/销售方分界线，将使用X坐标中点分割')
    
    # 根据分割线分配单词到各区域
    header_words = []
    buyer_words = []
    seller_words = []
    table_words = []
    
    LINE_TOLERANCE = 5.0  # 5像素容差
    
    for word in words:
        word_y = word['top']
        word_x = word['x0']
        
        # 发票头区域：在header_bottom_line上方
        if header_bottom_line and word_y > header_bottom_line['y0'] - LINE_TOLERANCE:
            header_words.append(word)
            continue
        
        # 表格区域：在table_top_line下方
        if table_top_line and word_y < table_top_line['y0'] + LINE_TOLERANCE:
            table_words.append(word)
            continue
        
        # 购销方区域：在header_bottom_line和table_top_line之间
        if header_bottom_line and table_top_line:
            if (header_bottom_line['y0'] - LINE_TOLERANCE > word_y > 
                table_top_line['y0'] + LINE_TOLERANCE):
                # 根据buyer_seller_divider区分购买方和销售方
                if buyer_seller_divider:
                    if word_x < buyer_seller_divider['x0']:
                        buyer_words.append(word)
                    else:
                        seller_words.append(word)
                else:
                    # 如果没有分界线，使用X坐标中点
                    mid_x = page_width / 2
                    if word_x < mid_x:
                        buyer_words.append(word)
                    else:
                        seller_words.append(word)
                continue
        
        # 如果分割线识别不完整，使用默认分配
        # 根据Y坐标和关键词判断
        if not header_bottom_line and not table_top_line:
            # 无法分区，所有单词都归入header（后续可以改进）
            header_words.append(word)
    
    logging.info(f'分区结果: 发票头={len(header_words)}个单词, '
                f'购买方={len(buyer_words)}个单词, '
                f'销售方={len(seller_words)}个单词, '
                f'表格={len(table_words)}个单词')
    
    return header_words, buyer_words, seller_words, table_words


def print_partition_content(
    header_words: List[dict],
    buyer_words: List[dict],
    seller_words: List[dict],
    table_words: List[dict]
):
    """
    打印各分区的内容
    
    Args:
        header_words: 发票头区域的单词
        buyer_words: 购买方区域的单词
        seller_words: 销售方区域的单词
        table_words: 表格区域的单词
    """
    print("\n" + "="*80)
    print("发票分区内容")
    print("="*80)
    
    # 发票头区域
    print("\n【发票头区域】")
    print("-" * 80)
    header_text = reconstruct_text_from_words(header_words)
    if header_text:
        print(header_text)
    else:
        print("(空)")
    
    # 购买方区域
    print("\n【购买方区域】")
    print("-" * 80)
    buyer_text = reconstruct_text_from_words(buyer_words)
    if buyer_text:
        print(buyer_text)
    else:
        print("(空)")
    
    # 销售方区域
    print("\n【销售方区域】")
    print("-" * 80)
    seller_text = reconstruct_text_from_words(seller_words)
    if seller_text:
        print(seller_text)
    else:
        print("(空)")
    
    # 表格区域
    print("\n【表格区域】")
    print("-" * 80)
    table_text = reconstruct_text_from_words(table_words)
    if table_text:
        print(table_text)
    else:
        print("(空)")
    
    print("\n" + "="*80)

