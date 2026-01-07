"""
文件操作服务
"""
import os
import shutil
import logging
from pathlib import Path
from typing import List
from datetime import datetime


def find_files(root_path: str) -> List[str]:
    """
    递归地查找指定目录下的所有 PDF 文件
    
    Args:
        root_path: 根目录路径
        
    Returns:
        PDF 文件路径列表
    """
    root = Path(root_path)
    pdf_files = list(root.glob('*.pdf'))
    # pdf_files.sort(key=lambda x: x.stat().st_mtime)

    invoice_list = [str(f) for f in pdf_files]
    invoice_list.sort()
    return invoice_list


def build_file_path(base_path: str, file_name: str) -> str:
    """
    拼装文件路径
    
    Args:
        base_path: 基础路径
        file_name: 文件名
        
    Returns:
        完整文件路径
    """
    file_path = os.path.join(base_path, file_name)
    logging.info(f"new file path = {file_path}")
    return file_path


def convert_date_to_path(date_str: str) -> str:
    """
    将中文日期字符串转换为 YYYY/MM 路径格式
    
    Args:
        date_str: 日期字符串，格式如 "2025年11月1日"
        
    Returns:
        路径字符串，格式如 "2025/11"
        
    Raises:
        ValueError: 日期格式不正确
    """
    try:
        # 预处理：将兼容性字符替换为标准字符
        normalized_date = (date_str
                           .replace('⽉', '月')  # 兼容性月字符 -> 标准月字符
                           .replace('⽇', '日')) # 兼容性日字符 -> 标准日字符
        # 解析日期：2025年11月1日
        date_obj = datetime.strptime(normalized_date, '%Y年%m月%d日')
        # 格式化为路径：2025/11
        return date_obj.strftime('%Y/%m')
    except ValueError as e:
        raise ValueError(f'日期字符串解析失败: {e}')


def move_file(source_path: str, base_path: str, date_str: str) -> None:
    """
    将文件从源路径移动到目标路径（按日期组织）
    
    Args:
        source_path: 待移动文件的完整路径
        base_path: 基础目录路径
        date_str: 日期字符串，用于组织目录结构
        
    Raises:
        ValueError: 日期格式不正确
        OSError: 文件移动失败
    """
    try:
        # 转换日期为路径
        path = convert_date_to_path(date_str)
    except ValueError as e:
        raise ValueError(f'无法转换日期为路径: {e}')

    # 确定目标文件的新完整路径
    file_name = os.path.basename(source_path)
    destination_dir = os.path.join(base_path, path)
    destination_path = os.path.join(destination_dir, file_name)

    # 检查目标目录是否存在，如果不存在则创建它
    os.makedirs(destination_dir, exist_ok=True)

    # 执行移动操作
    # 如果目标路径和源路径在同一文件系统，shutil.move 会执行快速的重命名操作
    # 如果在不同的文件系统，它会执行复制+删除
    try:
        shutil.move(source_path, destination_path)
        logging.info(f"文件 '{file_name}' 已成功移动到 '{destination_dir}'")
    except OSError as e:
        raise OSError(f'文件移动失败 (源: {source_path} -> 目标: {destination_path}): {e}')


