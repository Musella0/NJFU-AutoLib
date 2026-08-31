# -*- coding: utf-8 -*-
"""抓取图书馆各区域的平面图和座位坐标，供前端「按图选座」使用。

这些数据是静态的——桌子和座位号一年也不会挪一次，所以不放进抢座主流程：
需要时手动跑一遍，产物（static/floorplans/ 下的图和 seat_layout.json）提交进仓库。

用法：
    docker compose exec flask-api python -m utils.fetch_seat_layout 学号 密码

数据来自 ic-web 的两个接口：
    GET ic-web/sysInfo?sysType=2&sysValue={roomId}&sysKind=16  → 平面图相对路径
    GET ic-web/reserve?roomIds={roomId}&resvDates=D,D&sysKind=8 → 座位列表（含 coordinate）
coordinate 是相对平面图的百分比 "x,y[,座位号方向]"，直接当 CSS 的 left/top 用。
"""

import json
import os
import sys
from datetime import datetime, timedelta

from pymongo import MongoClient

from utils import config
from utils.library_system import LibrarySystem

# devProp 的位标记，与官方前端一致：置位才在选座图上显示
PAGE_SHOW = 2

OUT_DIR = os.path.join(config.PROJECT_ROOT, "static", "floorplans")
JSON_PATH = os.path.join(OUT_DIR, "seat_layout.json")

# 图片压到这个宽度就够用了，前端最大也只有几百 px 宽
MAX_IMAGE_WIDTH = 1600


def _shrink(raw: bytes) -> bytes:
    """有 Pillow 就压一压，没有就原样存——省几 MB 而已，不值得加依赖。"""
    try:
        import io

        from PIL import Image
    except ImportError:
        return raw
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width > MAX_IMAGE_WIDTH:
            h = round(img.height * MAX_IMAGE_WIDTH / img.width)
            img = img.resize((MAX_IMAGE_WIDTH, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80, optimize=True)
        return buf.getvalue() if buf.tell() < len(raw) else raw
    except Exception:
        return raw


def _known_locations() -> dict:
    """devName → 现有 devices 集合里的 location，用来把 ic-web 的区域名对回项目内的叫法。

    ic-web 管「二层A区」，项目里的座位表叫「二楼A区」，七层还拆成南北侧；
    与其手写一张对照表，不如按座位号反查，改了也不会错。
    """
    try:
        client = MongoClient(config.get_mongo_uri())
        devices = client[config.DB_NAME].devices.find({}, {"_id": 0, "devName": 1, "location": 1})
        mapping = {d["devName"]: d.get("location", "") for d in devices}
        client.close()
        return mapping
    except Exception as e:
        print(f"[warn] 读取 devices 失败，location 字段留空：{e}")
        return {}


def fetch(username: str, password: str) -> dict:
    lib = LibrarySystem(username, password, vpn_password=password)
    print("登录成功")

    def api(path, params=None):
        url = f"{lib.base_url}{path.lstrip('/')}{lib.vpn_suffix}"
        return lib.session.get(url, params=params, timeout=(10, 90))

    date = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    name_to_loc = _known_locations()
    os.makedirs(OUT_DIR, exist_ok=True)

    areas = []
    for floor in api("ic-web/seatMenu").json().get("data", []):
        for child in floor.get("children", []):
            areas.append({
                "roomId": child["id"],
                "name": child["name"],
                "floor": floor["name"],
                "floorId": floor["id"],
            })

    result = []
    for area in areas:
        room_id = area["roomId"]

        info = api("ic-web/sysInfo", {"sysType": 2, "sysValue": room_id, "sysKind": 16}).json()
        content = (info.get("data") or {}).get("content") or ""
        image = None
        if content:
            resp = api("ic-web/" + content)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                image = f"{room_id}{os.path.splitext(content)[1] or '.jpg'}"
                with open(os.path.join(OUT_DIR, image), "wb") as f:
                    f.write(_shrink(resp.content))

        devices = api("ic-web/reserve", {
            "roomIds": room_id,
            "resvDates": f"{date},{date}",
            "sysKind": 8,
        }).json().get("data") or []

        seats = []
        for dev in devices:
            coord = (dev.get("coordinate") or "").split(",")
            if len(coord) < 2 or not (dev.get("devProp", 0) & PAGE_SHOW):
                continue
            try:
                x, y = round(float(coord[0]), 2), round(float(coord[1]), 2)
            except ValueError:
                continue
            seats.append([dev.get("devName"), x, y])
        seats.sort(key=lambda s: s[0])

        locations = [name_to_loc.get(s[0]) for s in seats if name_to_loc.get(s[0])]
        area.update({
            "location": max(set(locations), key=locations.count) if locations else area["name"],
            "image": image,
            "open": [devices[0].get("openStart"), devices[0].get("openEnd")] if devices else None,
            "seats": seats,
        })
        result.append(area)
        print(f"  {room_id} {area['name']:<6s} 图:{str(image):<18s} 座位:{len(seats):>4d} "
              f"({area['location']})")

    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "areas": result}
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    total = sum(len(a["seats"]) for a in result)
    print(f"\n共 {len(result)} 个区域 / {total} 个座位，写入 {JSON_PATH}")
    return payload


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    fetch(sys.argv[1], sys.argv[2])
