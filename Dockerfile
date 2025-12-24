# 1. 指定基础镜像：使用 slim 版本可以大幅减小镜像体积，同时包含 Python 环境
FROM python:3.14
LABEL authors="todd"


# 2. 设置环境变量
# 防止 Python 产生 .pyc 编译文件
ENV PYTHONDONTWRITEBYTECODE=1
# 保证日志能实时输出，不会被缓存
ENV PYTHONUNBUFFERED=1

# 3. 设置容器内的工作目录
WORKDIR /app

# 4. 【关键步骤】先拷贝依赖文件
# 这样做是为了利用 Docker 的缓存机制：只要 requirements.txt 不变，就不会重新执行 pip install
COPY requirements.txt .

# 5. 安装依赖
RUN pip install --no-cache-dir -r requirements.txt
# -i https://pypi.tuna.tsinghua.edu.cn/simple
# 6. 拷贝项目剩余的所有代码
COPY . .

# 7. 指定启动命令（这只是默认命令，IntelliJ 运行调试时会覆盖它）
CMD ["python", "main.py"]