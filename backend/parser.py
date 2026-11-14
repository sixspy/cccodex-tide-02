import re
from datetime import datetime

STATION = re.compile(r"^[A-Z]{2}\d{2}$")
FLAGS = {"A": "实测", "M": "插补", "B": "可疑"}


def parse_telegram_line(line: str):
    parts = [part.strip() for part in line.strip().split("|")]
    if len(parts) != 4:
        return None, "列数不是四段"
    station, stamp, tide, flag = parts
    if not station:
        return None, "站码为空"
    if not STATION.match(station):
        return None, "站码应为两位字母加两位数字"
    if not stamp:
        return None, "观测时刻为空"
    # strptime 对位数不严格（九位也能凑出时刻），先卡死十二位再解析。
    if not re.fullmatch(r"[0-9]{12}", stamp):
        return None, "时间不是十二位数字"
    try:
        observed = datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        return None, "时间不是有效时刻"
    if not tide:
        return None, "潮位为空"
    if not re.fullmatch(r"-?\d+", tide):
        return None, "潮位不是整数厘米"
    centimeters = int(tide)
    if centimeters < -500 or centimeters > 2000:
        return None, "潮位超出量程"
    if not flag:
        return None, "质量旗为空"
    if flag not in FLAGS:
        return None, "质量旗不在 A、M、B"
    return {
        "station_code": station,
        "observed_at": observed,
        "tide_cm": centimeters,
        "quality_flag": flag,
        "flag_label": FLAGS[flag],
    }, None
