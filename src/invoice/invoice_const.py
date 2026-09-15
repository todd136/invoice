# 表格解析相关常量
TABLE_KEYWORDS = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']
MIN_HEADER_KEYWORDS = 4  # 表头行至少需要包含的关键词数量
COLUMN_X_TOLERANCE = 10  # 列X坐标范围扩展容差（像素）
WORD_DISTANCE_THRESHOLD = 30  # 拆分关键词的单词距离阈值（像素）
SUMMARY_ROW_KEYWORDS = ['合计', '价税合计', '备注', '开票人']  # 合计行关键词