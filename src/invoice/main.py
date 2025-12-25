"""
主程序入口
"""
import sys
import logging
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.invoice.invoice_processor import InvoiceProcessor
from src.invoice.logger_config import setup_logger


def main():
    """主函数"""
    base_path = '/Volumes/share/temp/receipt'

    # 设置日志系统
    # 全局日志级别：INFO
    # src.invoice 模块及其子模块日志级别：DEBUG（可以看到详细的调试信息）
    setup_logger(
        base_path,
        global_level=logging.INFO,
        module_levels={'src.invoice': logging.INFO}
    )

    # 创建发票处理器并执行批量处理
    processor = InvoiceProcessor(base_path)
    invoice_list = processor.process_batch()

    logging.info('程序处理完成，即将退出...')


if __name__ == '__main__':
    main()
