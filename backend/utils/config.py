# config.py
from dotenv import load_dotenv
import os
from pathlib import Path

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = Path(__file__).parent.parent

# 加载 .env 文件
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, '.env'))

# 服务器配置
SERVER_IP = os.getenv("SERVER_IP", "0.0.0.0:5004")
DB_IP = os.getenv("DB_IP", "mongo:27017")

# MongoDB 认证
MONGO_USER = os.getenv("MONGO_USER", "")
MONGO_PASS = os.getenv("MONGO_PASS", "")

def get_mongo_uri():
    """构建 MongoDB 连接 URI"""
    if MONGO_USER and MONGO_PASS:
        return f"mongodb://{MONGO_USER}:{MONGO_PASS}@{DB_IP}/?authSource=admin"
    return f"mongodb://{DB_IP}/"

# 日志配置
LOG_FILE = os.getenv("LOG_FILE", "logs/auto_lib.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# 确保日志目录存在
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 完整的日志文件路径
LOG_FILE = os.path.join(PROJECT_ROOT, LOG_FILE)

# 其他配置
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
RETRY_INTERVAL = int(os.getenv("RETRY_INTERVAL", "5"))

# 数据库配置
DB_NAME = "AutoLib"
FOLDER_PATH = os.path.join(PROJECT_ROOT, os.getenv("FOLDER_PATH", "座位信息"))

if __name__ == "__main__":
    print(f"Mongo URI: {get_mongo_uri()}")
    print(f"Log File Path: {LOG_FILE}")
    print(f"Folder Path: {FOLDER_PATH}")
