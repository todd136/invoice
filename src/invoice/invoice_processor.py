"""
发票处理流程控制器
负责批量处理发票的完整流程：扫描、解析、移动、导出
"""
import logging
from typing import List, Optional

from .invoice import Invoice
from .invoice_service import extract_invoice_by_table_and_text
from .file_service import find_files, move_file, build_file_path
from .excel_service import batch_export_to_excel


class InvoiceProcessor:
    """
    发票处理流程控制器
    
    负责协调各个服务模块，完成发票的批量处理流程：
    1. 扫描PDF文件
    2. 循环处理每张发票（解析、移动）
    3. Excel导出
    """
    
    def __init__(self, base_path: str):
        """
        初始化发票处理器
        
        Args:
            base_path: 基础路径，用于扫描PDF文件和保存结果
        """
        self.base_path = base_path
    
    def process_batch(self) -> List[Invoice]:
        """
        批量处理发票：扫描、解析、移动、导出
        
        Returns:
            成功解析的发票列表
        """
        logging.info(f'开始从 {self.base_path} 解析发票文件...')
        
        # 1. 扫描 base_path 路径下的发票文件
        try:
            invoice_file_list = find_files(self.base_path)
        except Exception as e:
            logging.error(f'扫描发票发生错误: {e}')
            return []
        
        logging.info(f'在当前目录 {self.base_path} 中，找到 {len(invoice_file_list)} 张发票...')
        if len(invoice_file_list) == 0:
            logging.info(f'在当前目录 {self.base_path} 未找到发票，程序将退出...')
            return []
        
        # 2. 循环处理每张发票
        invoice_list = []
        for invoice_file in invoice_file_list:
            invoice = self.process_single(invoice_file)
            if invoice:
                invoice_list.append(invoice)
        
        # 3. 验证处理结果
        if len(invoice_list) == 0:
            logging.error(f'在当前目录 {self.base_path} 中，找到 {len(invoice_file_list)} 张发票，'
                         f'但未能读取到发票信息，程序将退出...')
            return []
        
        # 4. 将发票内容导出到Excel文件
        excel_file = build_file_path(self.base_path, 'invoice.xlsx')
        logging.info(f'开始将发票内容写入文件 {excel_file}...')
        try:
            batch_export_to_excel(invoice_list, excel_file)
            logging.info(f'成功导出 {len(invoice_list)} 张发票到 {excel_file}')
        except Exception as e:
            logging.error(f'将发票写入文件 {excel_file} 失败: {e}')
            # 即使导出失败，也返回已解析的发票列表
        
        logging.info('批量处理完成')
        return invoice_list
    
    def process_single(self, pdf_path: str, move_file_after_parse: bool = True) -> Optional[Invoice]:
        """
        处理单张发票：解析、移动文件
        
        Args:
            pdf_path: PDF文件路径
            move_file_after_parse: 解析成功后是否移动文件（默认True）
        
        Returns:
            解析成功的Invoice对象，失败返回None
        """
        try:
            # 解析发票
            invoice = extract_invoice_by_table_and_text(pdf_path)
            
            # 将发票转移至日期路径下
            if move_file_after_parse and invoice.date:
                try:
                    logging.info(f'发票 {pdf_path} 解析完成，移动文件到相应位置...')
                    move_file(pdf_path, self.base_path, invoice.date)
                except Exception as e:
                    logging.error(f'移动文件 {pdf_path} 失败，程序将跳过该张发票: {e}')
                    # 文件移动失败不影响返回解析结果
            elif move_file_after_parse and not invoice.date:
                logging.warning(f'发票 {pdf_path} 未解析到日期，跳过文件移动')
            
            return invoice
            
        except Exception as e:
            logging.error(f'读取发票 {pdf_path} 发生错误: {e}')
            return None

