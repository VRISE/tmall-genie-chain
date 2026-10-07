"""天猫精灵的请求和响应。

请求：application/json 的 POST。
响应：必须是 JSON，而且 Content-Type 不能是 text —— 平台会反序列化失败。
      returnValue.reply 一定会被 TTS 念出来，没有「只回话不出声」这种选项。
"""
from __future__ import annotations

import json

RESULT_TYPES = ("RESULT", "ASK_INF", "CONFIRM")


def parse(raw: bytes):
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None


def slot_map(req: dict) -> dict:
    out = {}
    for e in req.get("slotEntities") or []:
        name = (e.get("intentParameterName") or "").split("(")[0]
        if name:
            out[name] = e.get("standardValue") or e.get("originalValue") or ""
    return out


def reply(text: str, result_type: str = "RESULT", execute_code: str = "SUCCESS",
           actions=None, asked=None, code: str = "0", select=None) -> bytes:
    v = {
        "reply": text,
        "comment": text,
        "resultType": result_type,
        "executeCode": execute_code,
        "properties": {},
    }
    if actions:
        v["actions"] = actions
    if asked:
        v["askedInfos"] = asked
    if select:
        v["selectParaInfo"] = select
    return json.dumps({"returnCode": code, "returnMessage": "", "returnValue": v},
                      ensure_ascii=False).encode("utf-8")
