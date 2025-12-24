"""
主程序入口
"""
import os
import sys
import logging
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.invoice.invoice_processor import InvoiceProcessor


def setup_log(base_path: str) -> None:
    """
    设置日志系统
    
    Args:
        base_path: 基础路径
    """
    log_dir = os.path.join(base_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    log_file_name = f"app_{datetime.now().strftime('%Y-%m-%d')}.log"
    log_file_path = os.path.join(log_dir, log_file_name)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    logging.info(f'日志文件路径: {log_file_path}')


def main():
    """主函数"""
    base_path = '/Volumes/share/temp/receipt'

    # 设置日志
    setup_log(base_path)

    # 创建发票处理器并执行批量处理
    processor = InvoiceProcessor(base_path)
    invoice_list = processor.process_batch()

    logging.info('程序处理完成，即将退出...')


if __name__ == '__main__':
    main()
