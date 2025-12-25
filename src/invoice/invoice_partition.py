"""
发票分区服务
基于分割线将发票分为不同区域（发票头、购买方、销售方、表格）
"""
import logging
import math
import re
from typing import List, Optional, Tuple


def group_words_by_y(words: List[dict], y_tolerance: float = 3.0) -> dict:
    """
    将单词按Y坐标分组（同一行的单词）
    
    Args:
        words: 单词列表
        y_tolerance: Y坐标容差，用于判断是否在同一行
    
    Returns:
        字典，key为Y坐标（四舍五入到容差），value为单词列表
    """
    lines_dict = {}
    for word in words:
        # 使用容差将相近Y坐标的单词归为同一行
        y_key = round(word['top'] / y_tolerance) * y_tolerance
        if y_key not in lines_dict:
            lines_dict[y_key] = []
        lines_dict[y_key].append(word)
    return lines_dict


def find_keyword_line(lines_dict: dict, keywords: List[str]) -> Optional[float]:
    """
    在行分组字典中查找包含关键词的行，返回其Y坐标
    
    Args:
        lines_dict: 按Y坐标分组的单词字典
        keywords: 关键词列表
    
    Returns:
        包含关键词的行的Y坐标，如果未找到返回None
    """
    for y, line_words in sorted(lines_dict.items()):
        line_text = ' '.join([w['text'] for w in line_words])
        if any(kw in line_text for kw in keywords):
            return y
    return None


def _find_header_keyword_y_range(words: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """
    查找发票头关键词的Y坐标范围
    
    Args:
        words: 单词列表
    
    Returns:
        (header_y_min, header_y_max)，如果未找到返回(None, None)
    """
    header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票']
    
    header_y_coords = []
    for word in words:
        if any(kw in word['text'] for kw in header_keywords):
            header_y_coords.append(word['top'])
    
    if not header_y_coords:
        logging.debug('未找到发票头关键词，无法识别发票头底部分割线')
        return None, None
    
    header_y_min = min(header_y_coords)  # 最下方的关键词（top值最小，最靠上）
    header_y_max = max(header_y_coords)  # 最上方的关键词（top值最大，最靠下）
    
    return header_y_min, header_y_max


def _find_buyer_seller_start_y(words: List[dict]) -> Optional[float]:
    """
    查找"购 销"这一行的Y坐标，作为发票头底部的参考
    
    Args:
        words: 单词列表
    
    Returns:
        "购 销"行的Y坐标，如果未找到返回None
    """
    buyer_seller_start_y = None
    for word in words:
        if '购' in word['text'] or '销' in word['text']:
            # 找到第一个"购"或"销"的位置
            if buyer_seller_start_y is None or word['top'] < buyer_seller_start_y:
                buyer_seller_start_y = word['top']
    
    return buyer_seller_start_y


def _search_horizontal_line_candidates(
    lines: List[dict],
    search_y_min: float,
    search_y_max: float,
    page_width: float,
    min_width_ratio: float = 0.7
) -> Tuple[List[dict], List[dict]]:
    """
    搜索水平线候选
    
    Args:
        lines: 线条列表
        search_y_min: 搜索Y范围最小值
        search_y_max: 搜索Y范围最大值
        page_width: 页面宽度
        min_width_ratio: 最小宽度比例（默认0.7，即70%）
    
    Returns:
        (candidates, horizontal_lines) - 候选线和所有水平线
    """
    candidates = []
    horizontal_lines = []
    
    for line in lines:
        # 水平线（y0和y1接近）
        if abs(line['y0'] - line['y1']) < 2.0:
            line_width = abs(line['x1'] - line['x0'])
            horizontal_lines.append({
                'line': line,
                'y0': line['y0'],
                'width': line_width
            })
            # 横跨大部分页面（>min_width_ratio）
            if line_width > page_width * min_width_ratio:
                # 在搜索范围内
                if search_y_min < line['y0'] < search_y_max:
                    candidates.append(line)
    
    return candidates, horizontal_lines


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
    # 找到发票头关键词的Y坐标范围
    header_y_min, header_y_max = _find_header_keyword_y_range(words)
    if header_y_min is None or header_y_max is None:
        return None
    
    # 尝试找到"购 销"这一行作为发票头底部的参考
    buyer_seller_start_y = _find_buyer_seller_start_y(words)
    
    logging.debug(f'发票头关键词Y坐标范围: min={header_y_min:.2f}, max={header_y_max:.2f}')
    if buyer_seller_start_y:
        logging.debug(f'购销方起始Y坐标: {buyer_seller_start_y:.2f} (购/销关键词)')
    logging.debug(f'搜索发票头底部分割线的Y范围: {header_y_max + 5:.2f} < y0 < {header_y_max + 80:.2f}')
    if buyer_seller_start_y:
        logging.debug(f'基于购销方起始位置，搜索范围: {header_y_max + 5:.2f} < y0 < {buyer_seller_start_y + 30:.2f}')
    logging.debug(f'要求线宽 > {page_width * 0.7:.2f} (页面宽度的70%)')
    
    # 确定搜索范围（扩大容差，以便识别更多分割线）
    search_y_min = header_y_max + 5
    # 扩大搜索范围：从10像素增加到30像素，以便识别线条3 (Y=101.77)
    search_y_max = buyer_seller_start_y + 30 if buyer_seller_start_y else header_y_max + 80
    
    # 搜索水平线候选（严格条件：宽度>70%）
    candidates, horizontal_lines = _search_horizontal_line_candidates(
        lines, search_y_min, search_y_max, page_width, min_width_ratio=0.7
    )
    
    # 如果严格条件找不到，放宽线宽要求（>50%）
    if not candidates:
        logging.debug('放宽线宽要求，尝试查找宽度>50%的水平线')
        candidates, _ = _search_horizontal_line_candidates(
            lines, search_y_min, search_y_max, page_width, min_width_ratio=0.5
        )
    
    # 打印所有水平线的信息
    if horizontal_lines:
        logging.debug(f'找到 {len(horizontal_lines)} 条水平线:')
        for hl in horizontal_lines:
            in_range = search_y_min < hl['y0'] < search_y_max
            width_ok = hl['width'] > page_width * 0.7
            status = "✓候选" if (in_range and width_ok) else ("×范围外" if not in_range else "×宽度不足")
            logging.debug(f'  线条 Y={hl["y0"]:.2f}, 宽度={hl["width"]:.2f}, '
                        f'X范围=[{hl["line"]["x0"]:.2f}, {hl["line"]["x1"]:.2f}] {status}')
    
    if candidates:
        # 选择最接近关键词的线（选择y0值最接近header_y_max的）
        candidates.sort(key=lambda l: abs(l['y0'] - header_y_max))
        logging.debug(f'找到 {len(candidates)} 条候选线，选择最接近header_y_max({header_y_max:.2f})的线')
        for i, cand in enumerate(candidates):
            logging.debug(f'  候选{i+1}: Y={cand["y0"]:.2f}, 距离={abs(cand["y0"] - header_y_max):.2f}')
        logging.debug(f'✓ 发票头底部分割线: Y={candidates[0]["y0"]:.2f} '
                    f'(距离header_y_max={abs(candidates[0]["y0"] - header_y_max):.2f})')
        return candidates[0]
    
    logging.debug('未找到发票头底部分割线（可能没有满足条件的水平线）')
    return None


def filter_bottom_lines_from_grouped_data(
    lines_dict: dict,
    table_bottom_y: Optional[float],
    table_bottom_line_y: Optional[float]
) -> dict:
    """
    从分组数据中删除底部内容（价税合计、备注等）
    
    Args:
        lines_dict: 按Y坐标分组的单词字典
        table_bottom_y: 表格底部关键词Y坐标
        table_bottom_line_y: 表格底部水平线Y坐标
    
    Returns:
        过滤后的分组数据
    """
    filtered_lines_dict = {}
    BOUNDARY_TOLERANCE = 5.0
    
    for y, line_words in lines_dict.items():
        # 检查这一行是否应该被过滤
        should_filter = False
        
        # 1. 检查是否包含底部关键词
        line_text = ' '.join([w['text'] for w in line_words])
        if any(kw in line_text for kw in ['价税合计', '合计', '备注', '注', '开票人', '收款人', '复核人',
                                          '购方开户银行', '销方开户银行', '银行账号', '开户银行']):
            should_filter = True
        
        # 2. 使用Y坐标过滤
        if not should_filter:
            if table_bottom_line_y and table_bottom_y:
                if table_bottom_line_y < table_bottom_y:
                    # 水平线在关键词上方，使用水平线过滤
                    if y > table_bottom_line_y:
                        should_filter = True
                else:
                    # 水平线在关键词下方或太靠下，使用关键词过滤
                    if y >= table_bottom_y - BOUNDARY_TOLERANCE:
                        should_filter = True
            elif table_bottom_line_y:
                if y > table_bottom_line_y:
                    should_filter = True
            elif table_bottom_y:
                if y >= table_bottom_y - BOUNDARY_TOLERANCE:
                    should_filter = True
        
        # 如果不需要过滤，保留这一行
        if not should_filter:
            filtered_lines_dict[y] = line_words
    
    return filtered_lines_dict


def partition_three_regions_from_grouped_data(
    lines_dict: dict,
    header_bottom_y: Optional[float],
    table_top_y: Optional[float]
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    基于分组数据+水平线，划分3大块（按行分配）
    
    策略：
    1. 优先使用关键词行作为边界（"开票日期"是发票头最后一行，"项目名称"是表格第一行）
    2. 水平线作为辅助判断
    3. 按行分配，确保整行数据归入同一区域
    
    Args:
        lines_dict: 按Y坐标分组的单词字典（已过滤底部内容）
        header_bottom_y: 发票头底部边界Y坐标（水平线）
        table_top_y: 表格顶部边界Y坐标（水平线）
    
    Returns:
        (header_words, buyer_seller_words, table_words)
    """
    header_words = []
    buyer_seller_words = []
    table_words = []
    
    BOUNDARY_TOLERANCE = 5.0
    
    # 定义关键词列表
    buyer_seller_keywords = ['名称', '购买方', '销售方', '统一社会信用代码', '纳税人识别号', '开户银行', '银行账号']
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额', '合计', '价税合计']
    header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票', '发票']
    
    # 识别关键边界行（用于确定区域边界）
    header_last_keyword = '开票日期'  # 发票头最后一行
    table_first_keyword = '项目名称'  # 表格第一行
    
    header_last_y = None
    table_first_y = None
    
    # 查找关键边界行
    for y, line_words in lines_dict.items():
        line_text = ' '.join([w['text'] for w in line_words])
        if header_last_keyword in line_text:
            if header_last_y is None or y > header_last_y:  # 选择最靠下的"开票日期"行
                header_last_y = y
        if table_first_keyword in line_text:
            if table_first_y is None or y < table_first_y:  # 选择最靠上的"项目名称"行
                table_first_y = y
    
    logging.debug(f'关键边界行: 发票头最后一行(开票日期)Y={header_last_y}, 表格第一行(项目名称)Y={table_first_y}')
    
    # 按Y坐标排序（从上到下）
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0])
    
    for y, line_words in sorted_lines:
        # 检查这一行是否包含关键词
        line_text = ' '.join([w['text'] for w in line_words])
        
        # 优先级1：使用关键边界行判断（最高优先级）
        # "开票日期"行及之前的所有行，归入发票头
        if header_last_y and y <= header_last_y:
            logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 发票头（优先级1：在开票日期行及之前）')
            header_words.extend(line_words)
            continue
        
        # "项目名称"行及之后的所有行，归入表格
        if table_first_y and y >= table_first_y:
            logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 表格（优先级1：在项目名称行及之后）')
            table_words.extend(line_words)
            continue
        
        # 优先级2：检查是否包含关键词（按行判断）
        # 注意：在关键边界行之间的行，优先使用关键词判断
        # 但需要排除已经被关键边界行判断的行
        # 重要：在关键边界行之间的行，优先检查购销方和表格关键词，最后才检查发票头关键词
        # 这样可以避免误判（如"名称"行被误判为发票头）
        
        # 2.1 优先检查购销方关键词（在关键边界行之间）
        if any(kw in line_text for kw in buyer_seller_keywords):
            logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 购销方（优先级2：包含购销方关键词）')
            buyer_seller_words.extend(line_words)
            continue
        
        # 2.2 检查表格关键词
        if any(kw in line_text for kw in table_keywords):
            # 如果包含表格关键词，且不在"项目名称"行之前，归入表格
            if not (table_first_y and y < table_first_y):
                logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 表格（优先级2：包含表格关键词）')
                table_words.extend(line_words)
                continue
        
        # 2.3 最后检查发票头关键词（避免误判）
        if any(kw in line_text for kw in header_keywords):
            # 如果包含发票头关键词，且不在"开票日期"行之后，归入发票头
            # 但需要排除"发票"这个通用关键词，因为它可能出现在其他地方
            if not (header_last_y and y > header_last_y):
                # 如果只包含"发票"关键词，需要进一步判断
                if '发票' in line_text and not any(kw in line_text for kw in ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票']):
                    # 只包含"发票"，可能是其他内容，不归入发票头
                    # 继续后续判断
                    pass
                else:
                    # 包含明确的发票头关键词，归入发票头
                    logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 发票头（优先级2：包含发票头关键词）')
                    header_words.extend(line_words)
                    continue
        
        # 优先级3：使用Y坐标判断（关键词判断失败时）
        # 在关键边界行之间的行，使用水平线边界判断
        if header_last_y and table_first_y:
            # 在"开票日期"行和"项目名称"行之间的行，归入购销方
            if header_last_y < y < table_first_y:
                logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 购销方（优先级3：在开票日期行和项目名称行之间）')
                buyer_seller_words.extend(line_words)
            elif y <= header_last_y:
                logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 发票头（优先级3：Y坐标判断）')
                header_words.extend(line_words)
            elif y >= table_first_y:
                logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 表格（优先级3：Y坐标判断）')
                table_words.extend(line_words)
            else:
                logging.debug(f'行Y={y:.2f} "{line_text[:50]}" -> 购销方（优先级3：默认）')
                buyer_seller_words.extend(line_words)
        elif header_bottom_y and y < header_bottom_y - BOUNDARY_TOLERANCE:
            header_words.extend(line_words)
        elif table_top_y and y > table_top_y + BOUNDARY_TOLERANCE:
            table_words.extend(line_words)
        elif header_bottom_y and table_top_y:
            if (header_bottom_y - BOUNDARY_TOLERANCE <= y <= 
                table_top_y + BOUNDARY_TOLERANCE):
                buyer_seller_words.extend(line_words)
            else:
                # 默认归入购销方
                buyer_seller_words.extend(line_words)
        else:
            # 默认归入购销方
            buyer_seller_words.extend(line_words)
    
    logging.debug(f'3个大块分区结果（基于分组数据）: 发票头={len(header_words)}个单词, '
                 f'购销方={len(buyer_seller_words)}个单词, 表格={len(table_words)}个单词')
    
    return header_words, buyer_seller_words, table_words


def find_table_bottom_boundary(words: List[dict]) -> Optional[float]:
    """
    查找表格底部边界（价税合计、备注等行的上方）
    
    Args:
        words: 提取的单词列表
    
    Returns:
        表格底部边界的Y坐标（top值），如果未找到返回None
    """
    # 识别表格底部关键词（价税合计、备注、开票人等）
    # 这些关键词出现在表格底部，应该被排除
    bottom_keywords = ['价税合计', '合计', '备注', '注', '开票人', '收款人', '复核人', 
                      '购方开户银行', '销方开户银行', '银行账号', '开户银行']
    
    # 找到这些关键词的最小Y坐标（最靠上的，即top值最小）
    bottom_y_coords = []
    for word in words:
        if any(kw in word['text'] for kw in bottom_keywords):
            # 排除表格中的"合计"（可能是金额合计）
            # 如果"合计"在"价税合计"附近，才认为是底部
            if '合计' in word['text'] and '价税' not in word['text']:
                # 检查附近是否有"价税合计"
                word_y = word['top']
                nearby_has_price_tax = False
                for other_word in words:
                    if '价税合计' in other_word['text']:
                        if abs(other_word['top'] - word_y) < 20:  # 20像素内
                            nearby_has_price_tax = True
                            break
                if not nearby_has_price_tax:
                    continue  # 跳过表格中的"合计"
            bottom_y_coords.append(word['top'])
    
    if bottom_y_coords:
        # 返回最小的Y坐标（最靠上的底部关键词）
        table_bottom = min(bottom_y_coords)
        # logging.debug(f'识别到表格底部边界: Y={table_bottom:.2f} (基于关键词: {bottom_keywords})')
        return table_bottom
    
    logging.debug('未找到表格底部关键词，不设置底部边界')
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
    # 在pdfplumber中，top值越大越靠下，所以表头的top值应该较大
    table_header_y = None
    for word in words:
        if any(kw in word['text'] for kw in table_keywords):
            if table_header_y is None or word['top'] > table_header_y:
                table_header_y = word['top']
    
    if not table_header_y:
        logging.debug('未找到表格表头关键词，无法识别表格顶部分割线')
        return None
    
    logging.debug(f'表格表头关键词Y坐标: {table_header_y:.2f}')
    logging.debug(f'搜索表格顶部分割线的Y范围: {table_header_y - 50:.2f} < y0 < {table_header_y - 5:.2f}')
    logging.debug(f'要求线宽 > {page_width * 0.7:.2f} (页面宽度的70%)')
    
    # 查找在表头上方、横跨页面的水平线
    # 在pdfplumber中，top值越大越靠下，所以分割线应该在表头上方（y0值应该更小）
    candidates = []
    for line in lines:
        if abs(line['y0'] - line['y1']) < 2.0:  # 水平线
            line_width = abs(line['x1'] - line['x0'])
            if line_width > page_width * 0.7:
                # 在表头上方（y0值应该更小，容差30像素）
                # 分割线应该在table_header_y上方，但不要太远
                if table_header_y - 50 < line['y0'] < table_header_y - 5:
                    candidates.append(line)
    
    if candidates:
        candidates.sort(key=lambda l: abs(l['y0'] - table_header_y))
        logging.debug(f'找到 {len(candidates)} 条候选线，选择最接近table_header_y({table_header_y:.2f})的线')
        for i, cand in enumerate(candidates):
            logging.debug(f'  候选{i+1}: Y={cand["y0"]:.2f}, 距离={abs(cand["y0"] - table_header_y):.2f}')
        logging.debug(f'✓ 表格顶部分割线: Y={candidates[0]["y0"]:.2f} '
                    f'(距离table_header_y={abs(candidates[0]["y0"] - table_header_y):.2f})')
        return candidates[0]
    
    logging.debug('未找到表格顶部分割线（可能没有满足条件的水平线）')
    return None


def _determine_y_range_by_keywords(
    words: List[dict],
    header_bottom_line: Optional[dict],
    table_top_line: Optional[dict]
) -> Optional[Tuple[float, float]]:
    """
    使用关键词确定Y范围（用于find_buyer_seller_divider）
    
    Args:
        words: 单词列表
        header_bottom_line: 发票头底部分割线
        table_top_line: 表格顶部分割线
    
    Returns:
        (y_min, y_max) 或 None
    """
    if header_bottom_line and table_top_line:
        # 使用分割线确定Y范围
        return (header_bottom_line['y0'], table_top_line['y0'])
    
    # 如果缺少分割线，尝试使用关键词确定Y范围
    if not words:
        return None
    
    buyer_seller_keywords = ['购买方', '销售方', '名称', '统一社会信用代码']
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额']
    
    buyer_seller_y_coords = []
    table_y_coords = []
    
    for word in words:
        if any(kw in word['text'] for kw in buyer_seller_keywords):
            buyer_seller_y_coords.append(word['top'])
        if any(kw in word['text'] for kw in table_keywords):
            table_y_coords.append(word['top'])
    
    if buyer_seller_y_coords and table_y_coords:
        y_range = (min(buyer_seller_y_coords), max(table_y_coords))
        logging.debug(f'使用关键词确定的Y范围: [{y_range[0]:.2f}, {y_range[1]:.2f}]')
        return y_range
    
    logging.debug('无法使用关键词确定Y范围，跳过购买方/销售方分界线识别')
    return None


def find_buyer_seller_divider(
    lines: List[dict], 
    header_bottom_line: Optional[dict], 
    table_top_line: Optional[dict], 
    page_width: float,
    words: Optional[List[dict]] = None
) -> Optional[dict]:
    """
    查找购买方与销售方的分界线（垂直线）
    
    Args:
        lines: 页面中的所有线条
        header_bottom_line: 发票头底部分割线
        table_top_line: 表格顶部分割线
        page_width: 页面宽度
        words: 单词列表（用于备选方案）
    
    Returns:
        分界线对象，如果未找到返回None
    """
    # 确定Y范围
    if not header_bottom_line:
        logging.debug('未找到发票头底部分割线，尝试使用关键词确定购买方/销售方区域')
    if not table_top_line:
        logging.debug('未找到表格顶部分割线，尝试使用关键词确定购买方/销售方区域')
    
    y_range = _determine_y_range_by_keywords(words, header_bottom_line, table_top_line)
    if y_range is None:
        return None
    
    mid_x = page_width / 2
    
    logging.debug(f'购买方/销售方区域Y范围: [{y_range[0]:.2f}, {y_range[1]:.2f}]')
    logging.debug(f'搜索购买方/销售方分界线的X范围: {mid_x - page_width * 0.15:.2f} < x0 < {mid_x + page_width * 0.15:.2f} (页面中点±15%)')
    
    candidates = []
    vertical_lines = []
    for line in lines:
        # 垂直线（x0和x1接近）
        if abs(line['x0'] - line['x1']) < 2.0:
            line_x = line['x0']
            line_y_range = (min(line['y0'], line['y1']), 
                           max(line['y0'], line['y1']))
            vertical_lines.append({
                'line': line,
                'x0': line_x,
                'y_range': line_y_range
            })
            # 在页面中间位置（±15%容差）
            if abs(line_x - mid_x) < page_width * 0.15:
                # 在购销方区域内
                if (line_y_range[0] < y_range[1] and 
                    line_y_range[1] > y_range[0]):
                    candidates.append(line)
    
    # 打印所有垂直线的信息
    if vertical_lines:
        logging.debug(f'找到 {len(vertical_lines)} 条垂直线:')
        for vl in vertical_lines:
            logging.debug(f'  X={vl["x0"]:.2f}, Y范围=[{vl["y_range"][0]:.2f}, {vl["y_range"][1]:.2f}], '
                         f'距离中点={abs(vl["x0"] - mid_x):.2f}')
    
    if candidates:
        # 选择最接近页面中点的线
        candidates.sort(key=lambda l: abs(l['x0'] - mid_x))
        logging.debug(f'找到购买方/销售方分界线，X坐标: {candidates[0]["x0"]:.2f}')
        return candidates[0]
    
    logging.debug('未找到购买方/销售方分界线')
    return None


def filter_relevant_lines(lines: List[dict], page_width: float, words: Optional[List[dict]] = None) -> List[dict]:
    """
    过滤出关键分割线，排除无关线条
    
    Args:
        lines: 页面中的所有线条
        page_width: 页面宽度
        words: 单词列表（用于识别底部边界，排除底部线条）
    
    Returns:
        过滤后的线条列表
    """
    relevant_lines = []
    
    # 识别表格底部边界（用于排除底部线条）
    table_bottom_boundary = None
    if words:
        table_bottom_boundary = find_table_bottom_boundary(words)
    
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
        
        # 4. 排除表格底部的线条（价税合计、备注等行的分割线）
        if table_bottom_boundary:
            # 对于水平线，如果Y坐标在底部边界附近或下方，排除
            if abs(line['y0'] - line['y1']) < 2.0:  # 水平线
                line_y = line['y0']
                # 如果线条在底部边界下方（Y值更大），排除
                if line_y >= table_bottom_boundary - 10:  # 10像素容差
                    logging.debug(f'排除底部线条: Y={line_y:.2f} (表格底部边界={table_bottom_boundary:.2f})')
                    continue
        
        relevant_lines.append(line)
    
    return relevant_lines


def is_header_content(word: dict) -> bool:
    """
    判断单词是否应该是发票头内容
    
    Args:
        word: 单词字典
    
    Returns:
        如果是发票头内容返回True
    """
    text = word['text']
    header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票', '发票']
    # 检查是否包含发票头关键词
    if any(kw in text for kw in header_keywords):
        return True
    # 检查是否是日期格式（开票日期）
    if re.search(r'\d{4}年\d{1,2}月\d{1,2}日', text):
        return True
    # 检查是否是发票号码（12-20位数字）
    if re.match(r'^\d{12,20}$', text.replace(' ', '')):
        return True
    return False


def is_bottom_content(word: dict) -> bool:
    """
    判断单词是否应该是发票底部内容（开票人、备注、银行信息等）
    
    Args:
        word: 单词字典
    
    Returns:
        如果是底部内容返回True
    """
    text = word['text']
    bottom_keywords = ['价税合计', '合计', '备注', '注', '开票人', '收款人', '复核人',
                      '购方开户银行', '销方开户银行', '银行账号', '开户银行', '账号']
    # 检查是否包含底部关键词
    if any(kw in text for kw in bottom_keywords):
        return True
    # 检查是否是"合计"（但排除表格中的金额合计）
    if '合计' in text and '价税' not in text:
        # 如果附近有"价税合计"，才认为是底部
        return True
    # 检查是否是银行账号格式（长数字串，通常是银行账号）
    if re.match(r'^\d{15,20}$', text.replace(' ', '')):
        # 可能是银行账号，需要结合上下文判断
        return True
    return False


def is_buyer_seller_content(word: dict) -> bool:
    """
    判断单词是否应该是购销方内容
    
    Args:
        word: 单词字典
    
    Returns:
        如果是购销方内容返回True
    """
    text = word['text']
    # 优先匹配完整关键词
    buyer_seller_keywords = ['购买方', '销售方', '名称', '统一社会信用代码', 
                            '纳税人识别号', '开户银行', '银行账号']
    if any(kw in text for kw in buyer_seller_keywords):
        return True
    # 匹配单字关键词（但排除明显不是的情况）
    single_char_keywords = ['购', '销', '买', '售']
    # 如果文本只包含这些单字，或者是"方"等，可能是购销方标签
    if len(text.strip()) <= 2 and any(kw in text for kw in single_char_keywords):
        return True
    # 检查是否是公司名称（包含"公司"、"企业"、"有限"等）
    if re.search(r'(公司|企业|有限|股份|集团|服务|信息|科技)', text):
        return True
    # 检查是否是税号格式（18位数字或字母数字组合）
    # 注意：排除发票号码格式（12-20位纯数字），发票号码应该在发票头区域
    cleaned_text = text.replace(' ', '')
    if re.match(r'^[0-9A-Z]{15,20}$', cleaned_text):
        # 如果是12-20位纯数字，可能是发票号码，不是税号
        if re.match(r'^\d{12,20}$', cleaned_text):
            return False  # 发票号码，不是税号
        # 否则是税号（包含字母或长度不在12-20位纯数字范围内）
        return True
    return False


def is_table_content(word: dict) -> bool:
    """
    判断单词是否应该是表格内容
    
    Args:
        word: 单词字典
    
    Returns:
        如果是表格内容返回True
    """
    text = word['text']
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额',
                     '合计', '价税合计', '*', '服务', '费']
    # 检查是否包含表格关键词
    if any(kw in text for kw in table_keywords):
        return True
    # 检查是否是项目名称模式（*项目名称*费用名称）
    if re.search(r'\*[^*]+\*[^*]+', text):
        return True
    return False


def _find_table_bottom_line(
    lines: List[dict],
    table_bottom_keyword_y: Optional[float],
    table_top_y: Optional[float],
    page_width: float
) -> Optional[float]:
    """
    查找表格底部水平线（用于确定表格底部边界）
    
    Args:
        lines: 线条列表
        table_bottom_keyword_y: 表格底部关键词的Y坐标（如"价税合计"）
        table_top_y: 表格顶部边界Y坐标
        page_width: 页面宽度
    
    Returns:
        表格底部水平线的Y坐标，如果未找到返回None
    """
    candidates = []
    
    # 方法1：如果有关键词，在关键词上方和下方都搜索
    if table_bottom_keyword_y:
        # 搜索范围：关键词上方150像素到下方50像素（扩大上方搜索范围）
        search_y_min = table_bottom_keyword_y - 150
        search_y_max = table_bottom_keyword_y + 50
        
        logging.debug(f'搜索表格底部水平线: 关键词Y={table_bottom_keyword_y:.2f}, 搜索范围=[{search_y_min:.2f}, {search_y_max:.2f}]')
        
        for line in lines:
            # 水平线（y0和y1接近）
            if abs(line['y0'] - line['y1']) < 2.0:
                line_width = abs(line['x1'] - line['x0'])
                # 横跨大部分页面（>70%）
                if line_width > page_width * 0.7:
                    # 在搜索范围内，且在表格顶部下方
                    if (search_y_min < line['y0'] < search_y_max and
                        (not table_top_y or line['y0'] > table_top_y)):
                        candidates.append(line)
                        logging.debug(f'找到候选水平线: Y={line["y0"]:.2f}, 宽度={line_width:.2f}')
    
    # 方法2：如果没有关键词或没找到，查找表格顶部下方、横跨页面的水平线
    # 找到所有在表格顶部下方的水平线，选择最靠下的（但要在关键词下方，如果有关键词）
    if not candidates and table_top_y:
        logging.debug(f'方法1未找到，使用方法2: 表格顶部Y={table_top_y:.2f}')
        for line in lines:
            if abs(line['y0'] - line['y1']) < 2.0:  # 水平线
                line_width = abs(line['x1'] - line['x0'])
                if line_width > page_width * 0.7:
                    # 在表格顶部下方，且距离表格顶部有一定距离（至少20像素）
                    if line['y0'] > table_top_y + 20:
                        # 如果有关键词，水平线应该在关键词下方（或附近，扩大范围到150像素）
                        if not table_bottom_keyword_y or line['y0'] < table_bottom_keyword_y + 150:
                            candidates.append(line)
                            logging.debug(f'找到候选水平线: Y={line["y0"]:.2f}, 宽度={line_width:.2f}')
    
    if candidates:
        # 如果有关键词，优先选择关键词上方最近的水平线（用于分隔表格内容和底部内容）
        # 如果选择最靠下的线，可能会太靠下，导致底部内容无法被过滤
        if table_bottom_keyword_y:
            # 筛选出关键词上方的水平线
            above_keyword_lines = [l for l in candidates if l['y0'] < table_bottom_keyword_y]
            if above_keyword_lines:
                # 选择关键词上方最近的水平线（y0值最大的，即最靠下的）
                above_keyword_lines.sort(key=lambda l: l['y0'], reverse=True)
                table_bottom_line_y = above_keyword_lines[0]['y0']
                logging.debug(f'识别到表格底部水平线: Y={table_bottom_line_y:.2f} '
                            f'(在关键词Y={table_bottom_keyword_y:.2f}上方，用于分隔表格内容和底部内容)')
                return table_bottom_line_y
            else:
                # 如果没有关键词上方的线，选择最靠下的线
                candidates.sort(key=lambda l: l['y0'], reverse=True)
                table_bottom_line_y = candidates[0]['y0']
                logging.debug(f'识别到表格底部水平线: Y={table_bottom_line_y:.2f} '
                            f'(所有候选线都在关键词下方，选择最靠下的)')
                return table_bottom_line_y
        else:
            # 没有关键词，选择最靠下的线
            candidates.sort(key=lambda l: l['y0'], reverse=True)
            table_bottom_line_y = candidates[0]['y0']
            logging.debug(f'识别到表格底部水平线: Y={table_bottom_line_y:.2f} '
                        f'(无关键词，选择最靠下的线)')
            return table_bottom_line_y
    
    logging.debug('未找到表格底部水平线')
    return None


def identify_region_boundaries(
    lines: List[dict], 
    words: List[dict], 
    page_width: float,
    all_lines: Optional[List[dict]] = None
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    识别3个大块的边界（Y坐标）
    结合水平线和行分组（关键词行）双重验证
    
    Args:
        lines: 过滤后的线条列表
        words: 单词列表
        page_width: 页面宽度
        all_lines: 所有线条列表（用于查找表格底部水平线，如果为None则使用lines）
    
    Returns:
        (header_bottom_y, table_top_y, table_bottom_y, table_bottom_line_y)
        table_bottom_line_y: 表格底部水平线的Y坐标（用于过滤底部内容）
    """
    # 1. 按行分组
    lines_dict = group_words_by_y(words, y_tolerance=3.0)
    
    # 2. 识别关键行（基于关键词）
    header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票']
    buyer_seller_keywords = ['购', '销', '购买方', '销售方']
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额']
    
    # 查找购销方开始行（"购 销"行）
    header_bottom_keyword_y = find_keyword_line(lines_dict, buyer_seller_keywords)
    
    # 查找表格开始行（"项目名称"行）
    table_top_keyword_y = find_keyword_line(lines_dict, table_keywords)
    
    logging.debug(f'基于行分组识别的边界: 购销方开始行Y={header_bottom_keyword_y}, 表格开始行Y={table_top_keyword_y}')
    
    # 3. 识别水平线
    header_bottom_line = find_header_bottom_line(lines, words, page_width)
    table_top_line = find_table_top_line(lines, words, page_width)
    
    # 4. 综合判断边界（优先使用水平线，如果缺失或位置不合理，使用关键词行）
    header_bottom_y = None
    if header_bottom_line:
        header_bottom_y = header_bottom_line['y0']
        logging.debug(f'使用水平线识别发票头底部边界: Y={header_bottom_y:.2f}')
    elif header_bottom_keyword_y:
        header_bottom_y = header_bottom_keyword_y
        logging.debug(f'使用关键词行识别发票头底部边界: Y={header_bottom_y:.2f}')
    
    table_top_y = None
    if table_top_line:
        table_top_y = table_top_line['y0']
        logging.debug(f'使用水平线识别表格顶部边界: Y={table_top_y:.2f}')
    elif table_top_keyword_y:
        table_top_y = table_top_keyword_y
        logging.debug(f'使用关键词行识别表格顶部边界: Y={table_top_y:.2f}')
    
    # 5. 验证边界合理性
    if header_bottom_y and table_top_y:
        if header_bottom_y >= table_top_y:
            # 边界不合理，使用关键词行
            logging.warning(f'边界不合理（header_bottom_y={header_bottom_y:.2f} >= table_top_y={table_top_y:.2f}），使用关键词行')
            if header_bottom_keyword_y:
                header_bottom_y = header_bottom_keyword_y
            if table_top_keyword_y:
                table_top_y = table_top_keyword_y
    
    # 6. 识别表格底部边界（关键词）
    table_bottom_y = find_table_bottom_boundary(words)
    
    # 7. 识别表格底部水平线（用于过滤底部内容）
    # 使用所有线条查找，因为过滤后的线条可能不包含底部水平线
    lines_for_bottom = all_lines if all_lines is not None else lines
    table_bottom_line_y = _find_table_bottom_line(lines_for_bottom, table_bottom_y, table_top_y, page_width)
    
    logging.debug(f'最终确定的3个大块边界: 发票头底部Y={header_bottom_y}, '
                 f'表格顶部Y={table_top_y}, 表格底部关键词Y={table_bottom_y}, '
                 f'表格底部水平线Y={table_bottom_line_y}')
    
    return header_bottom_y, table_top_y, table_bottom_y, table_bottom_line_y


def _check_same_line_for_keywords(
    word: dict,
    words: List[dict],
    keywords: List[str],
    y_tolerance: float = 3.0
) -> bool:
    """
    检查同一行是否有指定的关键词
    
    Args:
        word: 当前单词
        words: 所有单词列表
        keywords: 关键词列表
        y_tolerance: Y坐标容差
    
    Returns:
        如果同一行有指定关键词返回True
    """
    word_y = word['top']
    for w in words:
        if w == word:
            continue
        if abs(w['top'] - word_y) < y_tolerance:
            if any(kw in w['text'] for kw in keywords):
                return True
    return False


def _should_assign_to_header(
    word: dict,
    header_bottom_y: Optional[float],
    boundary_tolerance: float,
    all_words: Optional[List[dict]] = None
) -> bool:
    """
    判断单词是否应归入发票头区域
    
    Args:
        word: 单词
        header_bottom_y: 发票头底部边界Y坐标
        boundary_tolerance: 边界容差
        all_words: 所有单词列表（用于检查同一行的关键词）
    
    Returns:
        如果应归入发票头区域返回True
    """
    if not header_bottom_y:
        return False
    
    word_y = word['top']
    word_text = word['text']
    
    # 优先使用关键词判断：如果包含明显的购销方或表格关键词，排除
    if is_buyer_seller_content(word) and not is_header_content(word):
        return False
    if is_table_content(word) and not is_header_content(word):
        return False
    
    # 检查同一行是否有购销方关键词（如"名称："）
    # 即使当前单词本身不包含关键词，但如果同一行有"名称："等关键词，也应该排除
    if all_words:
        buyer_seller_keywords = ['名称', '购买方', '销售方', '统一社会信用代码', '纳税人识别号']
        if _check_same_line_for_keywords(word, all_words, buyer_seller_keywords):
            return False
        
        table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额']
        if _check_same_line_for_keywords(word, all_words, table_keywords):
            return False
    
    # 在发票头底部边界上方（使用坐标判断）
    if word_y < header_bottom_y - boundary_tolerance:
        return True
    
    return False


def _should_assign_to_table(
    word: dict,
    table_top_y: Optional[float],
    table_bottom_y: Optional[float],
    boundary_tolerance: float
) -> bool:
    """
    判断单词是否应归入表格区域
    
    Args:
        word: 单词
        table_top_y: 表格顶部边界Y坐标
        table_bottom_y: 表格底部边界Y坐标
        boundary_tolerance: 边界容差
    
    Returns:
        如果应归入表格区域返回True
    """
    if not table_top_y:
        return False
    
    word_y = word['top']
    # 在表格顶部边界下方
    if word_y > table_top_y + boundary_tolerance:
        # 排除表格底部内容
        if table_bottom_y and word_y >= table_bottom_y - boundary_tolerance:
            return False
        # 如果包含明显的购销方关键词，排除
        if is_buyer_seller_content(word) and not is_table_content(word):
            return False
        return True
    
    return False


def _should_assign_to_buyer_seller(
    word: dict,
    header_bottom_y: Optional[float],
    table_top_y: Optional[float],
    table_bottom_y: Optional[float],
    boundary_tolerance: float
) -> bool:
    """
    判断单词是否应归入购销方区域
    
    Args:
        word: 单词
        header_bottom_y: 发票头底部边界Y坐标
        table_top_y: 表格顶部边界Y坐标
        table_bottom_y: 表格底部边界Y坐标
        boundary_tolerance: 边界容差
    
    Returns:
        如果应归入购销方区域返回True
    """
    if not header_bottom_y or not table_top_y:
        return False
    
    word_y = word['top']
    # 在两个边界之间
    if (header_bottom_y - boundary_tolerance <= word_y <= 
        table_top_y + boundary_tolerance):
        # 排除底部内容
        if table_bottom_y and word_y >= table_bottom_y - boundary_tolerance:
            return False
        if is_bottom_content(word):
            return False
        return True
    
    return False


def partition_three_regions(
    words: List[dict],
    header_bottom_y: Optional[float],
    table_top_y: Optional[float],
    table_bottom_y: Optional[float],
    table_bottom_line_y: Optional[float] = None
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    将单词分配到3个大块
    
    Args:
        words: 单词列表
        header_bottom_y: 发票头底部边界Y坐标
        table_top_y: 表格顶部边界Y坐标
        table_bottom_y: 表格底部边界Y坐标（关键词）
        table_bottom_line_y: 表格底部水平线Y坐标（用于过滤底部内容）
    
    Returns:
        (header_words, buyer_seller_words, table_words)
    """
    header_words = []
    buyer_seller_words = []
    table_words = []
    
    BOUNDARY_TOLERANCE = 5.0  # 边界容差
    
    for word in words:
        word_y = word['top']
        word_text = word['text']
        
        # ========== 第一步：过滤底部内容（优先执行，确保所有底部内容都被过滤） ==========
        # 策略：结合Y坐标和关键词判断，确保所有底部内容都被过滤
        
        # 1. 优先检查是否是底部内容（使用关键词判断）
        if is_bottom_content(word):
            # 如果是底部内容，直接跳过，不归入任何区域
            logging.debug(f'单词"{word_text}" (Y={word_y:.2f}) 是底部内容，跳过')
            continue  # 不归入任何区域
        
        # 2. 使用Y坐标过滤（作为补充）
        # 如果表格底部水平线存在，且水平线在关键词上方（用于分隔表格内容和底部内容）
        #    则使用水平线过滤：Y坐标大于水平线的单词，直接跳过
        if table_bottom_line_y and table_bottom_y:
            if table_bottom_line_y < table_bottom_y:
                # 水平线在关键词上方，使用水平线过滤
                if word_y > table_bottom_line_y:
                    logging.debug(f'单词"{word_text}" (Y={word_y:.2f}) 在表格底部水平线(Y={table_bottom_line_y:.2f})下方，跳过')
                    continue  # 不归入任何区域
            else:
                # 水平线在关键词下方或太靠下，使用关键词过滤
                if word_y >= table_bottom_y - BOUNDARY_TOLERANCE:
                    logging.debug(f'单词"{word_text}" (Y={word_y:.2f}) 在表格底部关键词(Y={table_bottom_y:.2f})下方，跳过（水平线Y={table_bottom_line_y:.2f}太靠下）')
                    continue  # 不归入任何区域
        elif table_bottom_line_y:
            # 只有水平线，没有关键词，使用水平线过滤
            if word_y > table_bottom_line_y:
                logging.debug(f'单词"{word_text}" (Y={word_y:.2f}) 在表格底部水平线(Y={table_bottom_line_y:.2f})下方，跳过')
                continue  # 不归入任何区域
        elif table_bottom_y:
            # 只有关键词，使用关键词过滤
            # 策略：如果单词Y坐标大于等于表格底部关键词-容差，直接跳过（不归入任何区域）
            # 这样可以过滤掉"价税合计"、"备注"、"开票人"、"业务单号"等所有底部内容
            if word_y >= table_bottom_y - BOUNDARY_TOLERANCE:
                logging.debug(f'单词"{word_text}" (Y={word_y:.2f}) 在表格底部关键词(Y={table_bottom_y:.2f})下方，跳过')
                continue  # 不归入任何区域
        
        # ========== 第二步：区域分配（优先使用关键词判断，关键词优先于坐标） ==========
        
        # 定义关键词列表
        buyer_seller_keywords = ['名称', '购买方', '销售方', '统一社会信用代码', '纳税人识别号', '开户银行', '银行账号']
        table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额', '合计', '价税合计']
        header_keywords = ['发票号码', '开票日期', '电子发票', '普通发票', '增值税专用发票', '发票']
        
        # 优先级1：检查同一行是否有关键词（最高优先级）
        # 即使单词本身不包含关键词，如果同一行有关键词，也应该归入对应区域
        # 注意：检查顺序很重要，应该先检查发票头（因为发票号码可能被误判为税号）
        if _check_same_line_for_keywords(word, words, header_keywords):
            header_words.append(word)
            continue
        
        if _check_same_line_for_keywords(word, words, buyer_seller_keywords):
            buyer_seller_words.append(word)
            continue
        
        if _check_same_line_for_keywords(word, words, table_keywords):
            table_words.append(word)
            continue
        
        # 优先级2：检查单词本身是否包含关键词
        if is_buyer_seller_content(word):
            buyer_seller_words.append(word)
            continue
        
        if is_table_content(word):
            table_words.append(word)
            continue
        
        if is_header_content(word):
            header_words.append(word)
            continue
        
        # 优先级3：使用坐标判断（关键词判断失败时）
        # 按优先级判断应归入哪个区域（使用坐标）
        if _should_assign_to_header(word, header_bottom_y, BOUNDARY_TOLERANCE, words):
            header_words.append(word)
            continue
        
        if _should_assign_to_table(word, table_top_y, table_bottom_y, BOUNDARY_TOLERANCE):
            table_words.append(word)
            continue
        
        if _should_assign_to_buyer_seller(word, header_bottom_y, table_top_y, table_bottom_y, BOUNDARY_TOLERANCE):
            buyer_seller_words.append(word)
            continue
        
        # 优先级4：最后的备选方案（根据Y坐标推断）
        if header_bottom_y and word_y < header_bottom_y:
            header_words.append(word)
        elif table_top_y and word_y > table_top_y:
            table_words.append(word)
        else:
            # 默认归入购销方
            buyer_seller_words.append(word)
    
    logging.debug(f'3个大块分区结果: 发票头={len(header_words)}个单词, '
                 f'购销方={len(buyer_seller_words)}个单词, 表格={len(table_words)}个单词')
    
    return header_words, buyer_seller_words, table_words


def _assign_tax_id_by_name_position(
    word: dict,
    buyer_seller_words: List[dict],
    page_width: float
) -> Optional[str]:
    """
    根据同行的"名称"位置分配税号
    
    Args:
        word: 税号单词
        buyer_seller_words: 购销方区域的单词列表
        page_width: 页面宽度
    
    Returns:
        'buyer' 或 'seller'，如果无法判断返回None
    """
    word_y = word['top']
    word_x = word['x0']
    y_tolerance = 3.0
    
    # 查找同一行的其他单词
    same_line_words = [w for w in buyer_seller_words 
                     if abs(w['top'] - word_y) < y_tolerance 
                     and w != word]
    
    # 检查同一行是否有"名称"关键词
    name_word = None
    for w in same_line_words:
        if '名称' in w['text']:
            name_word = w
            break
    
    if name_word:
        # 如果税号在"名称"的左侧，归入购买方；右侧归入销售方
        return 'buyer' if word_x < name_word['x0'] else 'seller'
    
    # 如果没有找到"名称"，使用X坐标中点
    mid_x = page_width / 2
    return 'buyer' if word_x < mid_x else 'seller'


def _assign_word_by_keyword_or_position(
    word: dict,
    page_width: float
) -> str:
    """
    根据关键词或X坐标分配单词
    
    Args:
        word: 单词
        page_width: 页面宽度
    
    Returns:
        'buyer' 或 'seller'
    """
    word_x = word['x0']
    word_text = word['text']
    
    # 优先使用完整关键词判断
    if '购买方' in word_text or ('购' in word_text and '买' in word_text):
        return 'buyer'
    elif '销售方' in word_text or ('销' in word_text and '售' in word_text):
        return 'seller'
    elif '名称' in word_text:
        # 根据X坐标判断
        mid_x = page_width / 2
        return 'buyer' if word_x < mid_x else 'seller'
    else:
        # 使用X坐标中点
        mid_x = page_width / 2
        return 'buyer' if word_x < mid_x else 'seller'


def split_buyer_seller(
    buyer_seller_words: List[dict],
    lines: List[dict],
    header_bottom_y: Optional[float],
    table_top_y: Optional[float],
    page_width: float
) -> Tuple[List[dict], List[dict]]:
    """
    在购销方大块内拆分购买方和销售方
    
    Args:
        buyer_seller_words: 购销方区域的单词列表
        lines: 线条列表
        header_bottom_line: 发票头底部分割线（用于确定Y范围）
        table_top_line: 表格顶部分割线（用于确定Y范围）
        page_width: 页面宽度
    
    Returns:
        (buyer_words, seller_words)
    """
    # 构建虚拟的分割线对象（用于 find_buyer_seller_divider）
    header_bottom_line = None
    if header_bottom_y:
        header_bottom_line = {'y0': header_bottom_y, 'y1': header_bottom_y}
    
    table_top_line = None
    if table_top_y:
        table_top_line = {'y0': table_top_y, 'y1': table_top_y}
    
    # 查找购买方/销售方分界线
    buyer_seller_divider = find_buyer_seller_divider(
        lines, header_bottom_line, table_top_line, page_width, buyer_seller_words
    )
    
    buyer_words = []
    seller_words = []
    
    for word in buyer_seller_words:
        word_x = word['x0']
        word_text = word['text']
        
        if buyer_seller_divider:
            # 使用分界线区分
            if word_x < buyer_seller_divider['x0']:
                buyer_words.append(word)
            else:
                seller_words.append(word)
        else:
            # 使用关键词和X坐标结合判断
            # 检查是否是税号
            if re.match(r'^[0-9A-Z]{15,20}$', word_text.replace(' ', '')):
                # 这是税号，需要根据同一行的其他内容判断
                assignment = _assign_tax_id_by_name_position(word, buyer_seller_words, page_width)
                if assignment == 'buyer':
                    buyer_words.append(word)
                else:
                    seller_words.append(word)
            else:
                # 根据关键词或X坐标判断
                assignment = _assign_word_by_keyword_or_position(word, page_width)
                if assignment == 'buyer':
                    buyer_words.append(word)
                else:
                    seller_words.append(word)
    
    logging.debug(f'购销方拆分结果: 购买方={len(buyer_words)}个单词, 销售方={len(seller_words)}个单词')
    
    return buyer_words, seller_words


def _log_all_lines_info(all_lines: List[dict], page_width: float, table_bottom_boundary: Optional[float]):
    """
    打印所有线条的详细信息（用于调试）
    
    Args:
        all_lines: 所有线条列表
        page_width: 页面宽度
        table_bottom_boundary: 表格底部边界Y坐标
    """
    logging.debug("\n" + "="*80)
    logging.debug("线条坐标信息（所有线条）")
    logging.debug("="*80)
    logging.debug(f"{'序号':<6} {'类型':<10} {'X0':<10} {'X1':<10} {'Y0':<10} {'Y1':<10} {'宽度':<10} {'高度':<10} {'长度':<10} {'过滤原因':<15}")
    logging.debug("-" * 120)
    
    for i, line in enumerate(all_lines):
        line_type = "水平" if abs(line['y0'] - line['y1']) < 2.0 else "垂直" if abs(line['x0'] - line['x1']) < 2.0 else "斜线"
        x0 = line.get('x0', 0)
        x1 = line.get('x1', 0)
        y0 = line.get('y0', 0)
        y1 = line.get('y1', 0)
        width = abs(x1 - x0)
        height = abs(y1 - y0)
        length = max(width, height)
        
        # 分析为什么被过滤
        filter_reason = ""
        if line_type == "水平":
            line_length = math.sqrt((x1 - x0)**2 + (y1 - y0)**2)
            if line_length < page_width * 0.3:
                filter_reason = "太短"
            elif width < page_width * 0.7:
                filter_reason = f"宽度不足({width/page_width*100:.1f}%)"
            color = line.get('stroking_color')
            if color and len(color) == 3:
                if color[0] > 0.6 and color[1] < 0.4 and color[2] < 0.4:
                    filter_reason = "红色(印章)"
            if table_bottom_boundary and y0 >= table_bottom_boundary - 10:
                filter_reason = "底部边界"
        else:
            line_length = math.sqrt((x1 - x0)**2 + (y1 - y0)**2)
            if line_length < page_width * 0.3:
                filter_reason = "太短"
        
        logging.debug(f"{i+1:<6} {line_type:<10} {x0:<10.2f} {x1:<10.2f} {y0:<10.2f} {y1:<10.2f} {width:<10.2f} {height:<10.2f} {length:<10.2f} {filter_reason:<15}")
    
    logging.debug("="*80 + "\n")


def _log_filtered_lines(lines: List[dict]):
    """
    打印过滤后的线条信息（用于调试）
    
    Args:
        lines: 过滤后的线条列表
    """
    if lines:
        logging.debug("\n过滤后的线条坐标信息:")
        for i, line in enumerate(lines):
            line_type = "水平" if abs(line['y0'] - line['y1']) < 2.0 else "垂直" if abs(line['x0'] - line['x1']) < 2.0 else "斜线"
            logging.debug(f"  线条{i+1} ({line_type}): X=[{line['x0']:.2f}, {line['x1']:.2f}], Y=[{line['y0']:.2f}, {line['y1']:.2f}], "
                        f"宽度={abs(line['x1']-line['x0']):.2f}, 高度={abs(line['y1']-line['y0']):.2f}")


def _log_word_lines_grouping(lines_dict: dict):
    """
    打印单词按行分组的信息（用于调试）
    
    Args:
        lines_dict: 按Y坐标分组的单词字典
    """
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0], reverse=False)
    
    logging.debug("\n单词按行分组（用于分析区域边界）")
    logging.debug("="*80)
    logging.debug(f"{'行号':<6} {'Y坐标':<12} {'单词数':<8} {'文字内容'}")
    logging.debug("-" * 100)
    
    for line_num, (y_coord, line_words) in enumerate(sorted_lines, 1):
        sorted_line_words = sorted(line_words, key=lambda w: w['x0'])
        line_text = ' '.join([w['text'] for w in sorted_line_words])
        if len(line_text) > 80:
            line_text = line_text[:77] + "..."
        logging.debug(f"{line_num:<6} {y_coord:<12.2f} {len(line_words):<8} {line_text}")
    
    logging.debug("="*80)
    logging.debug(f"总共 {len(sorted_lines)} 行\n")


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


def partition_invoice_by_lines(page, words: List[dict]) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """
    通过分割线将发票分为4个区域（重构版本：分层分区逻辑）
    
    新逻辑：
    1. 保留筛选水平分割线的逻辑
    2. 将单词按行分组
    3. 用水平线+行分组，将单词划分为3个大块：发票头、购销方、表格
    4. 在购销方大块内，再拆分购买方和销售方
    
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
    logging.debug(f'页面中共有 {len(all_lines)} 条线条')
    
    # 识别表格底部边界（用于分析过滤原因）
    table_bottom_boundary = None
    if words:
        table_bottom_boundary = find_table_bottom_boundary(words)
    
    # 打印所有线条的坐标信息（用于调试）
    _log_all_lines_info(all_lines, page_width, table_bottom_boundary)
    
    # 过滤无关线条（排除底部线条）
    lines = filter_relevant_lines(all_lines, page_width, words)
    logging.debug(f'过滤后剩余 {len(lines)} 条相关线条（已排除底部价税合计/备注等行的分割线）')
    
    # 打印过滤后的线条信息
    _log_filtered_lines(lines)
    
    # ========== 新逻辑：分层分区 ==========
    logging.debug("\n" + "="*80)
    logging.debug("开始分层分区逻辑")
    logging.debug("="*80)
    
    # 步骤1：识别3个大块的边界（结合水平线和行分组）
    # 传递所有线条，用于查找表格底部水平线
    header_bottom_y, table_top_y, table_bottom_y, table_bottom_line_y = identify_region_boundaries(
        lines, words, page_width, all_lines
    )
    
    # 步骤2：将单词按行分组
    lines_dict = group_words_by_y(words, y_tolerance=3.0)
    _log_word_lines_grouping(lines_dict)
    
    # 步骤3：从分组数据中删除底部内容（价税合计、备注等）
    filtered_lines_dict = filter_bottom_lines_from_grouped_data(
        lines_dict, table_bottom_y, table_bottom_line_y
    )
    logging.debug(f'删除底部内容后，剩余 {len(filtered_lines_dict)} 行数据')
    
    # 步骤4：基于分组数据+水平线，划分3大块（按行分配）
    header_words, buyer_seller_words, table_words = partition_three_regions_from_grouped_data(
        filtered_lines_dict, header_bottom_y, table_top_y
    )
    
    # 步骤4：在购销方大块内拆分购买方和销售方
    buyer_words, seller_words = split_buyer_seller(
        buyer_seller_words, lines, header_bottom_y, table_top_y, page_width
    )
    
    logging.debug(f'\n最终分区结果: 发票头={len(header_words)}个单词, '
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

