"""
日志配置模块
提供统一的日志系统配置功能
"""
import os
import logging
from datetime import datetime
from typing import Optional, Dict


def setup_logger(
    base_path: str,
    global_level: int = logging.INFO,
    module_levels: Optional[Dict[str, int]] = None
) -> None:
    """
    设置日志系统
    
    Args:
        base_path: 基础路径，日志文件将保存在 base_path/logs 目录下
        global_level: 全局日志级别，默认为 logging.INFO
        module_levels: 模块级别的日志级别配置，格式为 {'模块名': 日志级别}
                       例如: {'src.invoice': logging.DEBUG, 'src.invoice.invoice_service': logging.INFO}
                       如果为 None，则使用默认配置 {'src.invoice': logging.DEBUG}
    
    Examples:
        # 使用默认配置（全局INFO，src.invoice模块DEBUG）
        setup_logger('/path/to/base')
        
        # 自定义全局和模块级别
        setup_logger(
            '/path/to/base',
            global_level=logging.WARNING,
            module_levels={'src.invoice': logging.DEBUG, 'src.invoice.invoice_service': logging.INFO}
        )
    """
    # 创建日志目录
    log_dir = os.path.join(base_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # 生成日志文件名（按日期）
    log_file_name = f"app_{datetime.now().strftime('%Y-%m-%d')}.log"
    log_file_path = os.path.join(log_dir, log_file_name)
    
    # 配置全局日志
    logging.basicConfig(
        level=global_level,
        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    logging.info(f'日志文件路径: {log_file_path}')
    logging.info(f'全局日志级别: {logging.getLevelName(global_level)}')
    
    # 设置模块级别的日志级别
    if module_levels is None:
        # 默认配置：src.invoice 模块及其子模块使用 DEBUG 级别
        module_levels = {'src.invoice': logging.DEBUG}
    
    for module_name, level in module_levels.items():
        logger = logging.getLogger(module_name)
        logger.setLevel(level)
        logging.info(f'模块 "{module_name}" 日志级别设置为: {logging.getLevelName(level)}')


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志记录器
    
    Args:
        name: 日志记录器名称，通常使用 __name__
    
    Returns:
        logging.Logger 实例
    
    Examples:
        logger = get_logger(__name__)
        logger.debug('调试信息')
        logger.info('普通信息')
    """
    return logging.getLogger(name)

