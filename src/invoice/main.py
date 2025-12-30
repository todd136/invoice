"""
主程序入口
"""
import sys
import os
import logging
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.invoice.invoice_processor import InvoiceProcessor
from src.invoice.logger_config import setup_logger

def get_executable_dir():
    """
    获取可执行文件所在目录
    
    如果使用PyInstaller等工具打包，sys.executable 指向打包后的可执行文件路径
    否则 sys.executable 指向 Python 解释器路径
    
    返回:
        Path: 可执行文件所在目录（打包后）或 Python 解释器所在目录（开发环境）
    """
    # sys.executable 在打包后指向可执行文件路径，在开发环境指向 Python 解释器路径
    # 获取其父目录即可得到可执行文件所在目录
    # executable_path = Path(sys.executable)
    executable_path = Path(sys.argv[0])
    return executable_path.parent.absolute()


def main():
    """主函数"""
    
    # 获取可执行文件所在目录（打包后）
    base_path = get_executable_dir()
    
    # 或者使用硬编码路径
    # base_path = '/Volumes/share/temp/receipt'
    print("executable path = "+ base_path)

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
    
    # 等待用户按键退出（适用于打包后的可执行文件）
    print('\n程序执行完成，按回车键退出...')
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        # 处理可能的输入错误（如重定向输入时）
        pass


if __name__ == '__main__':
    main()
