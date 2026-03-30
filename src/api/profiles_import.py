"""SillyTavern 角色卡导入 — POST /profiles/import/tavern

支持格式：
  - .json  V1 平铺 / V2 (spec: "chara_card_v2", data 嵌套)
  - .png   tEXt chunk 'chara' 字段，base64 → JSON（需要 Pillow）
  - 另见 POST /lorebooks/import/tavern：可上传 **SillyTavern 单独导出的世界书** JSON（根级含 entries，
    可为对象 { \"0\": {...} } 或数组），无需包在 character_book 内。

字段映射：
  name / data.name / character_name / nickname / nicknames[0] / 文件名 → display_name, profile_id（slugify）
  data.system_prompt (V2, 非空) → base_prompt (优先)
  description / data.description → base_prompt
  personality                   → 追加 【性格特点】
  scenario                      → 追加 【背景设定】
  mes_example                   → 追加 【对话示例】
  first_mes                     → 追加 【开场白（仅参考，不作为实际消息）】
  character_book.entries        → lorebooks/<id>.json + profile["lorebook_ref"]（供世界书 segment）

返回: {profile_id, display_name, message}
"""
import base64
import io
import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi import Request

from src.config.prompt_loader import get_locale

from src.lorebooks.store import new_lorebook_id, save_lorebook_document

logger = logging.getLogger(__name__)
router = APIRouter()

PROFILES_DIR = "profiles"
AVATARS_DIR  = "static/avatars"


# ── 解析工具 ──────────────────────────────────────────────────────────────────

def _extract_json_from_png(data: bytes) -> dict:
    """从 PNG tEXt chunk 'chara' 中提取 base64 JSON。需要 Pillow。"""
    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(
            status_code=422,
            detail="PNG 角色卡需要 Pillow：pip install Pillow>=10.0.0"
        )
    try:
        img = Image.open(io.BytesIO(data))
        chara_b64 = img.text.get("chara") or img.text.get("ccv3")
        if not chara_b64:
            raise HTTPException(status_code=422, detail="PNG 中未找到 'chara' 或 'ccv3' 文字块")
        return json.loads(base64.b64decode(chara_b64).decode("utf-8"))
    except HTTPException:
        raise  # 直接透传，不包装
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"PNG 角色卡 base64/JSON 解析失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PNG 读取失败: {e}")


def _normalize_card(raw: dict) -> dict:
    """将 V1 平铺或 V2/V3 嵌套统一成平铺结构。

    SillyTavern：chara_card_v2 / chara_card_v3 均为 spec + data 嵌套；
    部分导出会在顶层带 name，而 data 内省略，需合并。
    """
    spec = (raw.get("spec") or "")
    if spec.startswith("chara_card_v") and isinstance(raw.get("data"), dict):
        data = raw["data"]
        flat = {k: v for k, v in data.items()}
        flat.setdefault("character_book", raw.get("character_book"))
        # 顶层补充（V3 或部分工具只在外层写 name）
        for key in ("name", "character_name"):
            if not flat.get(key) and raw.get(key):
                flat[key] = raw[key]
        return flat
    return raw


def _extract_character_name(card: dict) -> str:
    """从常见 ST / CCV 字段解析角色显示名（不要求单一 name 键）。"""
    string_keys = (
        "name",
        "character_name",
        "char_name",
        "chara_name",
        "nickname",
        "nick",
    )
    for k in string_keys:
        v = card.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    nicks = card.get("nicknames")
    if isinstance(nicks, list):
        for n in nicks:
            if isinstance(n, str) and n.strip():
                return n.strip()
    # 少数卡用 character 存短名
    ch = card.get("character")
    if isinstance(ch, str) and ch.strip():
        return ch.strip()
    return ""


def _fallback_name_from_filename(filename: str) -> str:
    """无 name 时用上传文件名（去扩展名）作为显示名。"""
    base = os.path.splitext(os.path.basename(filename or ""))[0]
    base = base.strip()
    return base if base else ""


def _slugify(name: str) -> str:
    """将角色名转换为合法 profile_id（ASCII 字母/数字/下划线）。"""
    slug = re.sub(r"[^\w\u4e00-\u9fff]", "_", name.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "imported_char"


def _unique_profile_id(base_id: str) -> str:
    """若 profile_id 已存在，追加 _2, _3, ..."""
    candidate = base_id
    counter = 2
    while (
        os.path.exists(os.path.join(PROFILES_DIR, candidate + ".json"))
        or os.path.exists(os.path.join(PROFILES_DIR, candidate))
    ):
        candidate = f"{base_id}_{counter}"
        counter += 1
    return candidate


def _strip_html(text: str) -> str:
    """剥离 HTML 标签（SillyTavern 富文本编辑器产物），返回纯文本。"""
    from html import unescape
    # 移除 <img> 标签（含属性，无内容）
    text = re.sub(r"<img[^>]*>", "", text, flags=re.IGNORECASE)
    # 移除所有其他 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 解码 HTML 实体（&amp; &lt; 等）
    text = unescape(text)
    # 压缩连续空行
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    return text


def _build_base_prompt(card: dict, *, locale: str) -> str:
    """拼接 base_prompt：description + 各字段追加块。"""
    parts = []

    # 主体：V2 system_prompt 优先，否则 description
    system_prompt = _strip_html((card.get("system_prompt") or "").strip())
    description = _strip_html((card.get("description") or "").strip())

    if system_prompt:
        parts.append(system_prompt)
    elif description:
        parts.append(description)

    is_en = (locale or "zh") == "en"

    personality_label = "Personality" if is_en else "性格特点"
    scenario_label = "Scenario" if is_en else "背景设定"
    mes_example_label = "Dialogue Example" if is_en else "对话示例"
    first_mes_label = "Opening (Reference Only)" if is_en else "开场白（仅参考，不作为实际消息）"

    def _append(key: str, label: str):
        val = _strip_html((card.get(key) or "").strip())
        if val:
            parts.append(f"【{label}】\n{val}")

    _append("personality", personality_label)
    _append("scenario", scenario_label)
    _append("mes_example", mes_example_label)

    first_mes = _strip_html((card.get("first_mes") or "").strip())
    if first_mes:
        parts.append(f"【{first_mes_label}】\n{first_mes}")

    return "\n\n".join(parts)


def _iter_sorted_lorebook_entries(entries) -> list:
    """SillyTavern 的 entries 可能是数组，也可能是对象 { \"0\": {...}, \"1\": {...} }（独立世界书导出常见）。"""
    if isinstance(entries, dict):
        vals = [v for v in entries.values() if isinstance(v, dict)]

        def _sort_key(e: dict):
            u = e.get("uid")
            if u is None:
                u = e.get("id")
            if u is None:
                u = e.get("insertion_order", 0)
            try:
                return int(u)
            except (TypeError, ValueError):
                return 0

        vals.sort(key=_sort_key)
        return vals
    if isinstance(entries, list):
        return [x for x in entries if isinstance(x, dict)]
    return []


def _build_lorebook(card: dict) -> list:
    """解析 character_book.entries 或独立世界书 JSON 顶层的 entries，返回 lorebook 列表供动态 segment 使用。

    支持：
      - 角色卡内嵌：character_book.entries（数组或对象）
      - SillyTavern 单独导出的 world lore：根上直接有 name/description/entries，无 character_book

    SillyTavern 常驻条目判定：
      - entry.constant == true  (V2 明确字段)
      - entry.selective == false (不设关键词过滤，即"始终注入")
    这两种情况下即便 keys 为空也会保留并标记 constant=true。

    关键词字段：keys 或 key（ST 导出常用 key 数组）。
    """
    raw_entries = None
    cb = card.get("character_book")
    if isinstance(cb, dict) and cb.get("entries") is not None:
        raw_entries = cb["entries"]
    elif card.get("entries") is not None and isinstance(card.get("entries"), (dict, list)):
        # 独立导出的世界书 JSON（如 main_RWBY_world_info.json）
        raw_entries = card["entries"]
    else:
        return []

    entries = _iter_sorted_lorebook_entries(raw_entries)
    result = []
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if entry.get("disable"):
            continue
        keys = entry.get("keys") or entry.get("key") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        # 常驻判定：explicit constant 字段 OR selective=false（始终注入模式）
        selective = entry.get("selective", True)
        is_constant = bool(entry.get("constant")) or not selective
        if not is_constant and not keys:
            continue  # 关键词模式但无关键词 — 跳过
        result.append({
            "keys": keys,
            "content": content,
            "enabled": True,
            "constant": is_constant,
        })
    return result


def _save_avatar_png(profile_id: str, img_bytes: bytes) -> str:
    """将 PNG 字节存为 static/avatars/{profile_id}.png，返回相对 URL。"""
    os.makedirs(AVATARS_DIR, exist_ok=True)
    dest = os.path.join(AVATARS_DIR, f"{profile_id}.png")
    with open(dest, "wb") as f:
        f.write(img_bytes)
    return f"avatars/{profile_id}.png"


def _extract_avatar_from_card(card: dict, profile_id: str) -> str:
    """从 JSON 卡的 avatar 字段提取头像（base64 data URL），存盘后返回相对 URL。
    返回空字符串表示没有可用头像。
    """
    raw = (card.get("avatar") or "").strip()
    if not raw or raw == "none":
        return ""
    # base64 data URL: data:image/png;base64,<data>
    if raw.startswith("data:image/"):
        try:
            header, b64data = raw.split(",", 1)
            ext = "png"
            if "jpeg" in header or "jpg" in header:
                ext = "jpg"
            elif "webp" in header:
                ext = "webp"
            img_bytes = base64.b64decode(b64data)
            os.makedirs(AVATARS_DIR, exist_ok=True)
            dest = os.path.join(AVATARS_DIR, f"{profile_id}.{ext}")
            with open(dest, "wb") as f:
                f.write(img_bytes)
            return f"avatars/{profile_id}.{ext}"
        except Exception as e:
            logger.warning("[import/tavern] 头像 base64 解码失败，跳过: %s", e)
    return ""


def _build_profile(card: dict, profile_id: str, avatar_url: str = "", *, locale: str) -> dict:
    """从规范化的角色卡构建 Shikigami profile JSON（世界书另存 lorebooks/，见 import 流程）。"""
    display_name = (card.get("name") or profile_id).strip()
    base_prompt = _build_base_prompt(card, locale=locale)

    profile = {
        "profile_id": profile_id,
        "display_name": display_name,
        "avatar": avatar_url,
        "base_prompt": base_prompt,
        # Imported profiles 默认也预加载到聊天侧栏（与 /profiles/create 行为一致）
        "load_into_chat": True,
        "emotion_config": {},
        "memory_config": {},
        "reflection_config": {},
        "engine_overrides": {},
        "imported_from": "sillytavern",
        "imported_at": time.time(),
    }
    return profile


def _save_profile(profile_id: str, profile: dict):
    """将 profile JSON 写入 profiles/{id}.json，并确保 profiles/{id}/ 目录存在。"""
    os.makedirs(PROFILES_DIR, exist_ok=True)
    profile_dir = os.path.join(PROFILES_DIR, profile_id)
    os.makedirs(profile_dir, exist_ok=True)

    json_path = os.path.join(PROFILES_DIR, profile_id + ".json")
    tmp_path = json_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, json_path)
    logger.info("[import/tavern] 写入 %s", json_path)


# ── 端点 ─────────────────────────────────────────────────────────────────────

@router.post("/profiles/import/tavern", status_code=201)
async def import_tavern_card(request: Request, file: UploadFile = File(...)):
    """导入 SillyTavern V1/V2 角色卡（.json 或 .png）为 Shikigami profile。"""
    filename = file.filename or ""
    data = await file.read()

    is_png = filename.lower().endswith(".png")

    # 解析 JSON
    if is_png:
        raw = _extract_json_from_png(data)
    elif filename.lower().endswith(".json"):
        try:
            raw = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=422, detail=f"JSON 解析失败: {e}")
    else:
        raise HTTPException(status_code=415, detail="仅支持 .json 或 .png 文件")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="角色卡格式无效（根节点非对象）")

    card = _normalize_card(raw)

    name = _extract_character_name(card)
    if not name:
        name = _fallback_name_from_filename(filename)
    if not name:
        name = "Imported"

    # 写回统一字段，供 _build_profile、世界书与日志使用（原卡可能仅有 character_name 等）
    card["name"] = name

    base_id = _slugify(name)
    profile_id = _unique_profile_id(base_id)
    locale = get_locale()

    # ── 头像 ──────────────────────────────────────────────────────────────────
    avatar_url = ""
    if is_png:
        # PNG 卡本身即头像
        try:
            avatar_url = _save_avatar_png(profile_id, data)
        except Exception as e:
            logger.warning("[import/tavern] PNG 头像保存失败，跳过: %s", e)
    else:
        # JSON 卡：尝试从 card.avatar 字段提取 base64
        avatar_url = _extract_avatar_from_card(card, profile_id)

    profile = _build_profile(card, profile_id, avatar_url=avatar_url, locale=locale)

    lorebook_entries = _build_lorebook(card)
    if lorebook_entries:
        # 内嵌世界书文件名：优先上传文件名，与 POST /lorebooks/import/tavern 一致
        name_from_file = _fallback_name_from_filename(filename)
        slug_src = (name_from_file.strip() if name_from_file else "") or name
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", slug_src)[:28].strip("_") or "card"
        lb_id = new_lorebook_id(prefix=f"st_{slug}")
        save_lorebook_document(
            lb_id,
            {
                "lorebook_id": lb_id,
                "display_name": f"《{profile['display_name']}》角色卡世界书",
                "entries": lorebook_entries,
                "lorebook_settings": {"scan_turns": 10},
            },
        )
        profile["lorebook_ref"] = lb_id

    try:
        _save_profile(profile_id, profile)
    except Exception as e:
        logger.exception("[import/tavern] 保存失败")
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")

    # Ensure it's actually loaded into the sidebar chat list.
    # UI checks `load_into_chat` from profile JSON; without `add_session`,
    # the checkbox can appear enabled but the session won't show up.
    try:
        sm = getattr(request.app.state, "session_manager", None) if request else None
        if sm and profile.get("load_into_chat", True):
            # add_session overwrites any existing in-memory session with the same id.
            if not (hasattr(sm, "_sessions") and profile_id in sm._sessions):
                sm.add_session(profile_id, profile["display_name"])
    except Exception:
        # Hot-load failure shouldn't fail the import itself.
        pass

    lorebook_count = len(lorebook_entries) if lorebook_entries else 0
    constant_count = sum(1 for e in lorebook_entries if e.get("constant")) if lorebook_entries else 0
    keyword_count = lorebook_count - constant_count

    msg = f"成功导入「{profile['display_name']}」"
    if lorebook_count:
        parts = []
        if keyword_count:
            parts.append(f"{keyword_count} 条关键词")
        if constant_count:
            parts.append(f"{constant_count} 条常驻")
        msg += f"，世界书共 {lorebook_count} 条（{'、'.join(parts)}）"

    return {
        "profile_id": profile_id,
        "display_name": profile["display_name"],
        "avatar": avatar_url,
        "lorebook_count": lorebook_count,
        "lorebook_ref": profile.get("lorebook_ref") or "",
        "message": msg,
    }
