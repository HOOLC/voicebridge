from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml


SEED_TTS_2_VOICES: list[dict[str, str]] = [
    {"name": "Vivi 2.0", "id": "zh_female_vv_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "小何 2.0", "id": "zh_female_xiaohe_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "云舟 2.0", "id": "zh_male_m191_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "小天 2.0", "id": "zh_male_taocheng_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "刘飞 2.0", "id": "zh_male_liufei_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "魅力苏菲 2.0", "id": "zh_male_sophie_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "清新女声 2.0", "id": "zh_female_qingxinnvsheng_uranus_bigtts", "group": "豆包语音合成模型2.0", "language": "中文"},
    {"name": "知性灿灿 2.0", "id": "zh_female_cancan_uranus_bigtts", "group": "角色扮演", "language": "中文"},
    {"name": "撒娇学妹 2.0", "id": "zh_female_sajiaoxuemei_uranus_bigtts", "group": "角色扮演", "language": "中文"},
    {"name": "甜美小源 2.0", "id": "zh_female_tianmeixiaoyuan_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "甜美桃子 2.0", "id": "zh_female_tianmeitaozi_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "爽快思思 2.0", "id": "zh_female_shuangkuaisisi_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "佩奇猪 2.0", "id": "zh_female_peiqi_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "邻家女孩 2.0", "id": "zh_female_linjianvhai_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "少年梓辛/Brayan 2.0", "id": "zh_male_shaonianzixin_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "猴哥 2.0", "id": "zh_male_sunwukong_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "Tina老师 2.0", "id": "zh_female_yingyujiaoxue_uranus_bigtts", "group": "教育场景", "language": "中文"},
    {"name": "暖阳女声 2.0", "id": "zh_female_kefunvsheng_uranus_bigtts", "group": "客服场景", "language": "中文"},
    {"name": "儿童绘本 2.0", "id": "zh_female_xiaoxue_uranus_bigtts", "group": "有声阅读", "language": "中文"},
    {"name": "大壹 2.0", "id": "zh_male_dayi_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "黑猫侦探社咪仔 2.0", "id": "zh_female_mizai_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "鸡汤女 2.0", "id": "zh_female_jitangnv_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "魅力女友 2.0", "id": "zh_female_meilinvyou_uranus_bigtts", "group": "通用场景", "language": "中文"},
    {"name": "流畅女声 2.0", "id": "zh_female_liuchangnv_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "儒雅逸辰 2.0", "id": "zh_male_ruyayichen_uranus_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "儿童绘本", "id": "zh_female_xueayi_saturn_bigtts", "group": "有声阅读", "language": "中文"},
    {"name": "大壹", "id": "zh_male_dayi_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "黑猫侦探社咪仔", "id": "zh_female_mizai_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "鸡汤女", "id": "zh_female_jitangnv_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "魅力女友", "id": "zh_female_meilinvyou_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "流畅女声", "id": "zh_female_santongyongns_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "儒雅逸辰", "id": "zh_male_ruyayichen_saturn_bigtts", "group": "视频配音", "language": "中文"},
    {"name": "可爱女生", "id": "saturn_zh_female_keainvsheng_tob", "group": "角色扮演", "language": "中文"},
    {"name": "调皮公主", "id": "saturn_zh_female_tiaopigongzhu_tob", "group": "角色扮演", "language": "中文"},
    {"name": "爽朗少年", "id": "saturn_zh_male_shuanglangshaonian_tob", "group": "角色扮演", "language": "中文"},
    {"name": "天才同桌", "id": "saturn_zh_male_tiancaitongzhuo_tob", "group": "角色扮演", "language": "中文"},
    {"name": "知性灿灿", "id": "saturn_zh_female_cancan_tob", "group": "角色扮演", "language": "中文"},
    {"name": "轻盈朵朵 2.0", "id": "saturn_zh_female_qingyingduoduo_cs_tob", "group": "客服场景", "language": "中文"},
    {"name": "温婉珊珊 2.0", "id": "saturn_zh_female_wenwanshanshan_cs_tob", "group": "客服场景", "language": "中文"},
    {"name": "热情艾娜 2.0", "id": "saturn_zh_female_reqingaina_cs_tob", "group": "客服场景", "language": "中文"},
]


def load_catalog_yaml(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    voices = payload.get("voices")
    return voices if isinstance(voices, list) else []


def build_official_voice_catalog() -> list[dict[str, Any]]:
    voices = []
    for item in SEED_TTS_2_VOICES:
        voices.append(
            {
                "name": str(item["name"]).strip(),
                "id": str(item["id"]).strip(),
                "aliases": [],
                "description": f"tts-2.0 | {item['group']} | {item['language']}",
            }
        )
    return sorted(voices, key=lambda item: str(item.get("name", "")).lower())


def write_official_voice_catalog(path: Path, voices: list[dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "voices": voices,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
