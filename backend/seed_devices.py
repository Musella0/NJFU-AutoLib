"""
座位数据初始化脚本

读取 座位信息/ 目录下的 .txt 文件，将设备信息导入 MongoDB。
作为 docker-compose 中的一次性服务运行。
"""

import os
import sys
from pymongo import MongoClient
from utils import config

def insert_devices_from_folder(folder_path, mongo_uri, db_name):
    """遍历文件夹中的 .txt 文件，将座位信息写入 MongoDB"""

    client = MongoClient(mongo_uri)
    db = client[db_name]
    devices_col = db.devices

    if not os.path.isdir(folder_path):
        print(f"[错误] 座位信息目录不存在: {folder_path}")
        sys.exit(1)

    total = 0
    for fn in sorted(os.listdir(folder_path)):
        if not fn.lower().endswith(".txt"):
            continue

        filepath = os.path.join(folder_path, fn)
        location = fn[:-4]  # 去掉 .txt
        # 去掉末尾的两位数字（如 "三楼A区座位" -> "三楼A区座"）
        # 原项目逻辑保持一致
        location_short = location[:-2] if len(location) > 2 else location

        count = 0
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                dev_id = parts[0].split(":")[1].strip()
                dev_name = parts[1].split(":")[1].strip()

                devices_col.update_one(
                    {"devId": dev_id},
                    {"$set": {
                        "devId": dev_id,
                        "devName": dev_name,
                        "location": location_short
                    }},
                    upsert=True
                )
                count += 1
        total += count
        print(f"[OK] {fn} -> {count} 条座位记录")

    print(f"\n座位数据初始化完成，共导入 {total} 条记录")
    print(f"当前 devices 集合文档数: {devices_col.count_documents({})}")
    client.close()

if __name__ == "__main__":
    mongo_uri = config.get_mongo_uri()
    print(f"连接 MongoDB: {config.DB_IP}")
    insert_devices_from_folder(config.FOLDER_PATH, mongo_uri, config.DB_NAME)
